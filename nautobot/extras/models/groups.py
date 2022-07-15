"""Dynamic Groups Models."""

import logging
import urllib

import django_filters
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.urls import reverse
from django.utils.functional import cached_property

from nautobot.core.fields import AutoSlugField
from nautobot.core.models import BaseModel
from nautobot.core.models.generics import OrganizationalModel
from nautobot.extras.choices import DynamicGroupOperatorChoices
from nautobot.extras.querysets import DynamicGroupQuerySet, DynamicGroupMembershipQuerySet
from nautobot.extras.utils import extras_features
from nautobot.utilities.utils import get_filterset_for_model, get_form_for_model, get_route_for_model


logger = logging.getLogger(__name__)


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "graphql",
    "relationships",
    "webhooks",
)
class DynamicGroup(OrganizationalModel):
    """Dynamic Group Model."""

    name = models.CharField(max_length=100, unique=True, help_text="Dynamic Group name")
    slug = AutoSlugField(max_length=100, unique=True, help_text="Unique slug", populate_from="name")
    description = models.CharField(max_length=200, blank=True)
    content_type = models.ForeignKey(
        to=ContentType,
        on_delete=models.CASCADE,
        verbose_name="Object Type",
        help_text="The type of object for this Dynamic Group.",
    )
    filter = models.JSONField(
        encoder=DjangoJSONEncoder,
        editable=False,
        default=dict,
        help_text="A JSON-encoded dictionary of filter parameters for group membership",
    )
    children = models.ManyToManyField(
        "extras.DynamicGroup",
        help_text="Child DynamicGroups of filter parameters for group membership",
        through="extras.DynamicGroupMembership",
        through_fields=("parent_group", "group"),
        related_name="parents",
    )

    objects = DynamicGroupQuerySet.as_manager()

    clone_fields = ["content_type", "filter"]

    # This is used as a `startswith` check on field names, so these can be
    # explicit fields or just substrings.
    #
    # Currently this means skipping "search", custom fields, and custom relationships.
    #
    # FIXME(jathan): As one example, `DeviceFilterSet.q` filter searches in `comments`. The issue
    # really being that this field renders as a textarea and it's not cute in the UI. Might be able
    # to dynamically change the widget if we decide we do want to support this field.
    #
    # Type: tuple
    exclude_filter_fields = ("q", "cf_", "cr_", "cpf_", "comments")  # Must be a tuple

    class Meta:
        ordering = ["content_type", "name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Accessing this sets the dynamic attributes. Is there a better way? Maybe?
        getattr(self, "model")

    def __str__(self):
        return self.name

    def natural_key(self):
        return (self.slug,)

    @property
    def model(self):
        """
        Access to the underlying Model class for this group's `content_type`.

        This class object is cached on the instance after the first time it is accessed.
        """

        if getattr(self, "_model", None) is None:
            try:
                model = self.content_type.model_class()
            except models.ObjectDoesNotExist:
                model = None

            if model is not None:
                self._set_object_classes(model)

            self._model = model

        return self._model

    def _set_object_classes(self, model):
        """
        Given the `content_type` for this group, dynamically map object classes to this instance.
        Protocol for return values:

        - True: Model and object classes mapped.
        - False: Model not yet mapped (likely because of no `content_type`)
        """

        # If object classes have already been mapped, return True.
        if getattr(self, "_object_classes_mapped", False):
            return True

        # Try to set the object classes for this model.
        try:
            self.filterset_class = get_filterset_for_model(model)
            self.filterform_class = get_form_for_model(model, form_prefix="Filter")
            self.form_class = get_form_for_model(model)
        # We expect this to happen on new instances or in any case where `model` was not properly
        # available to the caller, so always fail closed.
        except TypeError:
            logger.debug("Failed to map object classes for model %s", model)
            self.filterset_class = None
            self.filterform_class = None
            self.form_class = None
            self._object_classes_mapped = False
        else:
            self._object_classes_mapped = True

        return self._object_classes_mapped

    @cached_property
    def _map_filter_fields(self):
        """Return all FilterForm fields in a dictionary."""

        # Fail gracefully with an empty dict if nothing is working yet.
        if not self._set_object_classes(self.model):
            return {}

        # Get model form and fields
        modelform = self.form_class()
        modelform_fields = modelform.fields

        # Get filter form and fields
        filterform = self.filterform_class()
        filterform_fields = filterform.fields

        # Get filterset and fields
        filterset = self.filterset_class()
        filterset_fields = filterset.filters

        # Get dynamic group filter field mappings (if any)
        dynamic_group_filter_fields = getattr(self.model, "dynamic_group_filter_fields", {})

        # Model form fields that aren't on the filter form
        missing_fields = set(modelform_fields).difference(filterform_fields)

        # Try a few ways to see if a missing field can be added to the filter fields.
        for missing_field in missing_fields:
            # Skip excluded fields
            if missing_field.startswith(self.exclude_filter_fields):
                logger.debug("Skipping excluded form field: %s", missing_field)
                continue

            # In some cases, fields exist in the model form AND by another name in the filter form
            # (e.g. model form: `cluster` -> filterset: `cluster_id`) yet are omitted from the
            # filter form (e.g. filter form has "cluster_id" but not "cluster"). We only want to add
            # them if-and-only-if they aren't already in `filterform_fields`.
            if missing_field in dynamic_group_filter_fields:
                mapped_field = dynamic_group_filter_fields[missing_field]
                if mapped_field in filterform_fields:
                    logger.debug(
                        "Skipping missing form field %s; mapped to %s filter field", missing_field, mapped_field
                    )
                    continue

            # If the missing field isn't even in the filterset, move on.
            try:
                filterset_field = filterset_fields[missing_field]
            except KeyError:
                logger.debug("Skipping %s: doesn't have a filterset field", missing_field)
                continue

            # Get the missing model form field so we can use it to add to the filterform_fields.
            modelform_field = modelform_fields[missing_field]

            # Replace the modelform_field with the correct type for the UI. At this time this is
            # only being done for CharField since in the filterset form this ends up being a
            # `MultiValueCharField` (dynamically generated from from `MultiValueCharFilter`) which is
            # not correct for char fields.
            if isinstance(modelform_field, forms.CharField):
                # Get ready to replace the form field w/ correct widget.
                new_modelform_field = filterset_field.field
                new_modelform_field.widget = modelform_field.widget

                # If `required=True` was set on the model field, pop "required" from the widget
                # attributes. Filter fields should never be required!
                if modelform_field.required:
                    new_modelform_field.widget.attrs.pop("required")

                modelform_field = new_modelform_field

            # Carry over the `to_field_name` to the modelform_field.
            to_field_name = filterset_field.extra.get("to_field_name")
            if to_field_name is not None:
                modelform_field.to_field_name = to_field_name

            logger.debug("Added %s (%s) to filter fields", missing_field, modelform_field.__class__.__name__)
            filterform_fields[missing_field] = modelform_field

        # Reduce down to a final dict of desired fields.
        return_fields = {}
        for field_name, filter_field in filterform_fields.items():
            # Skip excluded fields
            if field_name.startswith(self.exclude_filter_fields):
                logger.debug("Skipping excluded filter field: %s", field_name)
                continue

            return_fields[field_name] = filter_field

        return return_fields

    def get_filter_fields(self):
        """Return a mapping of `{field_name: filter_field}` for this group's `content_type`."""
        # Fail cleanly until the object has been created.
        if self.model is None:
            return {}

        if not self.present_in_database:
            return {}

        return self._map_filter_fields

    def get_queryset(self):
        """
        Return a queryset for the `content_type` model of this group.

        The queryset is generated based on the `filterset_class` for the Model.
        """

        model = self.model

        if model is None:
            raise RuntimeError(f"Could not determine queryset for model '{model}'")

        filterset = self.filterset_class(self.filter, model.objects.all())
        qs = filterset.qs

        # Make sure that this instance can't be a member of its own group.
        if self.present_in_database and model == self.__class__:
            qs = qs.exclude(pk=self.pk)

        return qs

    @property
    def members(self):
        """Return the member objects for this group."""
        return self.get_group_queryset()

    @property
    def count(self):
        """Return the number of member objects in this group."""
        return self.get_group_queryset().count()

    def get_absolute_url(self):
        return reverse("extras:dynamicgroup", kwargs={"slug": self.slug})

    @property
    def members_base_url(self):
        """Return the list route name for this group's `content_type'."""
        if self.model is None:
            return ""

        route_name = get_route_for_model(self.model, "list")
        return reverse(route_name)

    def get_group_members_url(self):
        """Get URL to group members."""
        if self.model is None:
            return ""

        url = self.members_base_url
        filter_str = urllib.parse.urlencode(self.filter, doseq=True)

        if filter_str is not None:
            url += f"?{filter_str}"

        return url

    def set_filter(self, form_data):
        """
        Set all desired fields from `form_data` into `filter` dict.

        :param form_data:
            Dict of filter parameters, generally from a filter form's `cleaned_data`
        """
        # Get the authoritative source of filter fields we want to keep.
        filter_fields = self.get_filter_fields()

        # Populate the filterset from the incoming `form_data`. The filterset's internal form is
        # used for validation, will be used by us to extract cleaned data for final processing.
        filterset_class = self.filterset_class
        filterset_class.form_prefix = "filter"
        filterset = filterset_class(form_data)

        # Use the auto-generated filterset form perform creation of the filter dictionary.
        filterset_form = filterset.form

        # It's expected that the incoming data has already been cleaned by a form. This `is_valid()`
        # call is primarily to reduce the fields down to be able to work with the `cleaned_data` from the
        # filterset form, but will also catch errors in case a user-created dict is provided instead.
        if not filterset_form.is_valid():
            raise ValidationError(filterset_form.errors)

        # Perform some type coercions so that they are URL-friendly and reversible, excluding any
        # empty/null value fields.
        new_filter = {}
        for field_name in filter_fields:
            field = filterset_form.fields[field_name]
            field_value = filterset_form.cleaned_data[field_name]

            if isinstance(field, forms.ModelMultipleChoiceField):
                field_to_query = field.to_field_name or "pk"
                new_value = [getattr(item, field_to_query) for item in field_value]

            elif isinstance(field, forms.ModelChoiceField):
                field_to_query = field.to_field_name or "pk"
                new_value = getattr(field_value, field_to_query, None)

            else:
                new_value = field_value

            # Don't store empty values like `None`, [], etc.
            if new_value in (None, "", [], {}):
                logger.debug("[%s] Not storing empty value (%s) for %s", self.name, field_value, field_name)
                continue

            logger.debug("[%s] Setting filter field {%s: %s}", self.name, field_name, field_value)
            new_filter[field_name] = new_value

        self.filter = new_filter

    # FIXME(jathan): Yes, this is "something", but there is discrepancy between explicitly declared
    # fields on `DeviceFilterForm` (for example) vs. the `DeviceFilterSet` filters. For example
    # `Device.name` becomes a `MultiValueCharFilter` that emits a `MultiValueCharField` which
    # expects a list of strings as input. The inverse is not true. It's easier to munge this
    # dictionary when we go to send it to the form, than it is to dynamically coerce the form field
    # types coming and going... For now.
    def get_initial(self):
        """
        Return an form-friendly version of `self.filter` for initial form data.

        This is intended for use to populate the dynamically-generated filter form created by
        `generate_filter_form()`.
        """
        filter_fields = self.get_filter_fields()
        initial_data = self.filter.copy()

        # Brute force to capture the names of any `*CharField` fields.
        char_fields = [f for (f, ftype) in filter_fields.items() if ftype.__class__.__name__.endswith("CharField")]

        # Iterate the char fields and coerce their type to a singular value or
        # an empty string in the case of an empty list.
        for char_field in char_fields:
            if char_field not in initial_data:
                continue

            field_value = initial_data[char_field]

            if isinstance(field_value, list):
                # Either the first (and should be only) item in this list.
                if field_value:
                    new_value = field_value[0]
                # Or empty string if there isn't.
                else:
                    new_value = ""
                initial_data[char_field] = new_value

        return initial_data

    def generate_filter_form(self):
        """
        Generate a `FilterForm` class for use in `DynamicGroup` edit view.

        This form is used to popoulate and validate the filter dictionary.

        If a form cannot be created for some reason (such as on a new instance when rendering the UI
        "add" view), this will return `None`.
        """
        filter_fields = self.get_filter_fields()

        # FIXME(jathan): Account for field_order in the newly generated class.
        try:

            class FilterForm(self.filterform_class):
                prefix = "filter"

                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self.fields = filter_fields

        except (AttributeError, TypeError):
            return None

        return FilterForm

    def clean_filter(self):
        """Clean for `self.filter` that uses the filterset_class to validate."""
        if not isinstance(self.filter, dict):
            raise ValidationError({"filter": "Filter must be a dict"})

        # Accessing `self.model` will determine if the `content_type` is not correctly set, blocking validation.
        if self.model is None:
            raise ValidationError({"filter": "Filter requires a `content_type` to be set"})

        # Validate against the filterset's internal form validation.
        filterset = self.filterset_class(self.filter)
        if not filterset.is_valid():
            raise ValidationError(filterset.errors)

    def clean(self):
        super().clean()

        if self.present_in_database:
            # Check immutable fields
            database_object = self.__class__.objects.get(pk=self.pk)

            if self.content_type != database_object.content_type:
                raise ValidationError({"content_type": "ContentType cannot be changed once created"})

        # Validate `filter` dict
        self.clean_filter()

    def generate_query_for_filter(self, filter_field, value):
        """
        Return a `Q` object generated from a `filter_field` and `value`.

        :param filter_field:
            Filter instance
        :param value:
            Value passed to the filter
        """
        query = models.Q()

        # In this case we want all values in a set union (boolean OR) because we want ANY of the
        # filter values to match.
        if isinstance(filter_field, django_filters.MultipleChoiceFilter):
            for v in value:
                query |= models.Q(**filter_field.get_filter_predicate(v))

        # The method `get_filter_predicate()` is only available on instances or subclasses
        # of `MultipleChoiceFilter`, so we must construct a lookup if a filter is not
        # multiple-choice. This is safe for singular filters except `ModelChoiceFilter`, because they
        # do not support `to_field_name`.
        else:
            field_name = filter_field.field_name

            # Attempt to account for `ModelChoiceFilter` where `to_field_name` MAY be set.
            to_field_name = getattr(filter_field.field, "to_field_name", None)
            if to_field_name is not None:
                field_name = f"{field_name}__{to_field_name}"

            lookup = f"{field_name}__{filter_field.lookup_expr}"
            query |= models.Q(**{lookup: value})

        return query

    def generate_query_for_group(self, group):
        """
        Return a `Q` object generated from all filters for a `group`.

        :param group:
            DynamicGroup instance
        """
        fs = group.filterset_class(group.filter, group.get_queryset())
        query = models.Q()

        # In this case we want all filters for a group's filter dict in a set intersection (boolean
        # AND) because ALL filter conditions must match for the filter parameters to be valid.
        for field_name, value in fs.data.items():
            filter_field = fs.filters[field_name]
            query &= self.generate_query_for_filter(filter_field, value)

        return query

    def get_filter_q(self):
        """
        Return a `Q` object generated this groups's filters.
        """
        return self.generate_query_for_group(self)

    def get_children_q(self, base_q=None):
        """
        Return a `Q` object generated recursively from all nested filters for this dynamic group, starting with optionally provided `Q` object.

        :param base_q:
            Q instance. If not set, defaults to `models.Q()`.
        """
        if base_q is None:
            base_q = models.Q()
        query = base_q
        for membership in self.dynamic_group_memberships.all():
            group = membership.group
            operator = membership.operator
            next_set = group.generate_query()
            query = self.perform_membership_set_operation(group, operator, query, next_set)
        return query

    def perform_membership_set_operation(self, group, operator, query, next_set):
        """
        Perform set operation for a group membership. The `operator` and `next_set` are used to
        decide the appropriate action to take on the `query`. The updated `Q` object is returned.

        :param group:
            DynamicGroup instance
        :param operator:
            DynamicGroupOperatorChoices choice (str)
        :param query:
            Q instance
        :param next_set:
            Q instance
        """
        if operator == "union":
            query |= next_set
        elif operator == "difference":
            query &= ~next_set
        elif operator == "intersection":
            query &= next_set

        return query

    def generate_query(self):
        """
        Return a `Q` object generated recursively from all nested filters for this dynamic group,
        based from a `Q` object of this dynamic group's filter definition.
        """
        return self.get_children_q(base_q=self.get_filter_q())

    def get_group_queryset(self):
        """Return a filtered queryset of all descendant groups."""
        query = self.generate_query()
        qs = self.get_queryset()
        return qs.filter(query)

    def add_child(self, child, operator, weight):
        """
        Add a child group including `operator` and `weight`.

        :param child:
            DynamicGroup instance
        :param operator:
            DynamicGroupOperatorChoices choice value used to dictate filtering behavior
        :param weight:
            Integer weight used to order filtering
        """
        instance = self.children.through(parent_group=self, group=child, operator=operator, weight=weight)
        return instance.validated_save()

    def remove_child(self, child):
        """
        Remove a child group.

        :param child:
            DynamicGroup instance
        """
        instance = self.children.through.objects.get(parent_group=self, group=child)
        return instance.delete()

    def get_descendants(self):
        """
        Recursively return a list of the children of all child groups.
        """
        return self._get_relatives("children")

    def get_ancestors(self):
        """
        Recursively return a list of the parents of all parent groups.
        """
        return self._get_relatives("parents")

    def _get_relatives(self, related_attr):
        """
        Recursively return a list of the related groups of the given relation.

        :param related_attr:
            Direction to traverse, either "parents" or "children".
        """

        if related_attr not in ["parents", "children"]:
            return []

        relatives = []
        for related_group in getattr(self, related_attr).all():
            logger.debug("Processing group %s...", related_group)
            relatives.append(related_group)
            if getattr(self, related_attr, None):
                relatives.extend(related_group._get_relatives(related_attr))

        return relatives

    def get_siblings(self):
        """Return groups that share the same parents."""
        return DynamicGroup.objects.filter(parents__in=self.parents.all()).exclude(slug=self.slug)

    def is_root(self):
        """Return whether this is a root node (has children, but no parents)."""
        return self.children.exists() and not self.parents.exists()

    def is_leaf(self):
        """Return whether this is a leaf node (has parents, but no children)."""
        return self.parents.exists() and not self.children.exists()

    def get_ancestors_queryset(self):
        """Return a queryset of all ancestors."""
        pks = [obj.pk for obj in self.get_ancestors()]
        return self.ordered_queryset_from_pks(pks)

    def get_descendants_queryset(self):
        """Return a queryset of all descendants."""
        pks = [obj.pk for obj in self.get_descendants()]
        return self.ordered_queryset_from_pks(pks)

    def ancestors_tree(self):
        """
        Return a nested mapping of ancestors with the following structure:

            {
                parent_1: {
                    grandparent_1: {},
                    grandparent_2: {},
                },
                parent_2: {
                    grandparent_3: {
                        greatgrandparent_1: {},
                    },
                    grandparent_4: {},
                }
            }

        Each key is a `DynamicGroup` instance.
        """
        tree = {}
        for f in self.parents.all():
            tree[f] = f.ancestors_tree()

        return tree

    def flatten_ancestors_tree(self, tree):
        """
        Recursively flatten a tree mapping of ancestors to a list, adding a `depth attribute to each
        instance in the list that can be used for visualizing tree depth.

        :param tree:
            Output from `ancestors_tree()`
        """
        return self._flatten_tree(tree, descending=False)

    def descendants_tree(self):
        """
        Return a nested mapping of descendants with the following structure:

        {
            child_1: {
                grandchild_1: {},
                grandchild_2: {},
            },
            child_2: {
                grandchild_3: {
                    great_grand_child_1: {},
                }
            }
        }

        Each key is a `DynamicGroup` instance.
        """
        tree = {}
        for f in self.children.all():
            tree[f] = f.descendants_tree()

        return tree

    def flatten_descendants_tree(self, tree):
        """
        Recursively flatten a tree mapping of descendants to a list, adding a `depth attribute to each
        instance in the list that can be used for visualizing tree depth.

        :param tree:
            Output from `descendats_tree()`
        """
        return self._flatten_tree(tree, descending=True)

    def _flatten_tree(self, tree, descending=True, nodes=None, depth=1, idx=0):
        """
        Recursively flatten a tree mapping to a list.

        Adds attributes to each instance:
        - `depth` - Can be used for visualizing tree depth.
        - `my_idx` - Can be used for sorting. Each depth shifts the index order of magnitude down: 1, .1, .01, etc.

        :param tree:
            A nested dictionary tree
        :param descending:
            Whether to traverse descendants or ancestors. If not set, defaults to descending.
        :param nodes:
            An ordered list used to hold the flattened nodes
        :param depth:
            The tree traversal depth
        :param idx:
            The current index of the tree
        """

        if nodes is None:
            nodes = []

        if descending:
            method = "get_descendants"
        else:
            method = "get_ancestors"

        tree_as_enumeration = enumerate(tree)
        for (i, item) in tree_as_enumeration:
            my_idx = idx + ((i + 1) / pow(10, (depth - 1)))
            item.depth = depth
            item.my_idx = my_idx
            nodes.append(item)
            branches = getattr(item, method)()
            self._flatten_tree(branches, nodes=nodes, descending=descending, depth=depth + 1, idx=my_idx)

        return nodes

    def _ordered_filter(self, queryset, field_names, values):
        """
        Filters the provided `queryset` using `{field_name}__in` expressions for each field_name in the
        list of `field_names`. The query constructed by this method explicitly orders the results in
        the same order as the provided `values` using their list index. This is ideal for
        maintaining ordering of topologically sorted nodes.

        For example, the following would return an ordered queryset following the order in the list
        of "pk" values:

            self._ordered_filter(self.__class__.objects, ["pk"], pk_list)

        :param queryset:
            QuerySet object
        :param field_names:
            List of field names
        :param values:
            Ordered list of values corresponding values used to establish the queryset
        """
        if not isinstance(field_names, list):
            raise TypeError("Field names must be a list")

        case = []

        # This is queryset magic to build a query that explicitly orders the items in the list of
        # values based on their index value (idx). It's how we can get an explicitly ordered
        # queryset that can be used for topological sort, but also supports queryset filtering.
        for idx, value in enumerate(values):
            when_condition = {field_names[0]: value, "then": idx}
            case.append(models.When(**when_condition))

        order_by = models.Case(*case)
        filter_condition = {field_name + "__in": values for field_name in field_names}

        return queryset.filter(**filter_condition).order_by(order_by)

    def ordered_queryset_from_pks(self, pk_list):
        """
        Generates a queryset ordered by the provided list of primary keys.

        :param pk_list:
            Ordered list of primary keys
        """
        return self._ordered_filter(self.__class__.objects, ["pk"], pk_list)


class DynamicGroupMembership(BaseModel):
    """Intermediate model for associating filters to groups."""

    group = models.ForeignKey("extras.DynamicGroup", on_delete=models.CASCADE, related_name="+")
    parent_group = models.ForeignKey(
        "extras.DynamicGroup", on_delete=models.CASCADE, related_name="dynamic_group_memberships"
    )
    operator = models.CharField(choices=DynamicGroupOperatorChoices.CHOICES, max_length=12)
    weight = models.PositiveSmallIntegerField()

    objects = DynamicGroupMembershipQuerySet.as_manager()

    class Meta:
        unique_together = ["group", "parent_group", "operator", "weight"]
        ordering = ["parent_group", "weight", "group"]

    def __str__(self):
        return f"{self.group}: {self.operator} ({self.weight})"

    def natural_key(self):
        return self.group.natural_key() + self.parent_group.natural_key() + (self.operator, self.weight)

    natural_key.dependencies = ["extras.dynamicgroup"]

    @property
    def name(self):
        """Return the group name."""
        return self.group.name

    @property
    def slug(self):
        """Return the group slug."""
        return self.group.slug

    @property
    def filter(self):
        """Return the group filter."""
        return self.group.filter

    @property
    def members(self):
        """Return the group members."""
        return self.group.members

    @property
    def count(self):
        """Return the group count."""
        return self.group.count

    def get_absolute_url(self):
        """Return the group's absolute URL."""
        return self.group.get_absolute_url()

    def get_group_members_url(self):
        """Return the group members URL."""
        return self.group.get_group_members_url()

    def clean(self):
        super().clean()

        # Enforce matching content_type
        if self.parent_group.content_type != self.group.content_type:
            raise ValidationError({"group": "ContentType for group and parent_group must match"})

        # Assert that loops cannot be created (such as adding root parent as a nested child).
        if self.parent_group == self.group:
            raise ValidationError({"group": "Cannot add group as a child of itself"})

        if self.group in self.parent_group.get_ancestors():
            raise ValidationError({"group": "Cannot add ancestor as a child"})
