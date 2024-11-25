"""This module contains custom ViewSet classes."""
from __future__ import annotations

import json
import logging
from typing import Protocol, runtime_checkable

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.http import QueryDict
from rest_framework import exceptions, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.renderers import BaseRenderer, BrowsableAPIRenderer
from rest_framework.request import Request
from rest_framework.response import Response

from dynamic_rest.conf import settings
from dynamic_rest.filters import DynamicFilterBackend, DynamicSortingFilter
from dynamic_rest.metadata import DynamicMetadata
from dynamic_rest.pagination import DynamicPageNumberPagination
from dynamic_rest.processors import SideloadingProcessor
from dynamic_rest.utils import is_truthy

UPDATE_REQUEST_METHODS = ("PUT", "PATCH", "POST")
DELETE_REQUEST_METHOD = "DELETE"
PATCH = "PATCH"

logger = logging.getLogger(__name__)


def _extract_object_params(
    request: Request, name: str, raw: bool = False
) -> dict | None:
    """Extract object params, return as dict.

    Always returns None if raw??? # TODO: Make sure this makes sense.

    Args:
        request: Request object.
        name: Name of the object.
        raw: Raw flag.

    Returns:
        dict | None: Parsed object params.
    """
    params = request.query_params.lists()
    logger.debug("Extracting object params: %s", name)
    params_map = {}
    original_name = name
    prefix = name[:-1]
    offset = len(prefix)
    logger.debug("Name: %s, Prefix: %s, Offset: %s", name, prefix, offset)

    for param_name, value in params:
        if param_name == original_name:
            if raw and value:
                # filter{} as object
                return json.loads(value[0])
            else:
                continue

        if not param_name.startswith(prefix):
            continue

        if param_name.endswith("}"):
            param_name = param_name[offset:-1]
        elif param_name.endswith("}[]"):
            # strip off trailing }[]
            # this fixes an Ember queryparams issue
            param_name = param_name[offset:-3]
        else:
            # malformed argument like:
            # filter{foo=bar
            raise exceptions.ParseError(
                f'"{param_name}" is not a well-formed filter key.'
            )
        if param_name.endswith(".in"):
            temp_value = []
            for v in value:
                if v.startswith("[") and v.endswith("]") and "," in v:
                    v = v[1:-1].split(",")
                    temp_value.extend(v)
                else:
                    temp_value.append(v)
            value = temp_value
        params_map[param_name] = value
    logger.debug("Extracted object params: %s", params_map)
    return None if raw else params_map


def handle_encodings(request: Request) -> QueryParams:
    """Handle encodings.

    WSGIRequest does not support Unicode values in the query string.
    WSGIRequest handling has a history of drifting behavior between
    combinations of Python versions, Django versions and DRF versions.
    Django changed its QUERY_STRING handling here:
    https://goo.gl/WThXo6. DRF 3.4.7 changed its behavior here:
    https://goo.gl/0ojIIO.

    Args:
        request: Request object.

    Returns:
        QueryParams: Query parameters.
    """
    try:
        return QueryParams(request.GET)
    except UnicodeEncodeError:
        pass

    s = request.environ.get("QUERY_STRING", "")
    try:
        s = s.encode("utf-8")
    except UnicodeDecodeError:
        pass
    return QueryParams(s)


class QueryParams(QueryDict):
    """Extension of Django's QueryDict.

    Instantiated from a DRF Request object, and returns
     a mutable QueryDict subclass. Also adds methods that
     might be useful for our use-case.
    """

    def __init__(self, query_params, *args, **kwargs):
        """Initialize the QueryParams object."""
        if hasattr(query_params, "urlencode"):
            query_string = query_params.urlencode()
        else:
            assert isinstance(query_params, (str, bytes))
            query_string = query_params
        kwargs["mutable"] = True
        super().__init__(query_string, *args, **kwargs)

    def add(self, key, value):
        """Add a key/value pair to the QueryDict.

        Method to accept a list of values and append to flat list.
        QueryDict.appendlist(), if given a list, will append the list,
        which creates nested lists. In most cases, we want to be able
        to pass in a list (for convenience) but have it appended into
        a flattened list.
        TODO: Possibly throw an error if add() is used on a non-list param.
        """
        if isinstance(value, list):
            for val in value:
                self.appendlist(key, val)
        else:
            self.appendlist(key, value)


@runtime_checkable
class HasRequestProperty(Protocol):
    """Protocol for request property."""

    request: Request


@runtime_checkable
class HasInitializeRequest(Protocol):
    """Protocol for initialize_request method."""

    # pylint: disable=W0246
    def initialize_request(self, request: Request, *args, **kwargs) -> Request:
        """Initialize the request object."""
        return super().initialize_request(request, *args, **kwargs)


@runtime_checkable
class HasGetRenderers(Protocol):
    """Protocol for get_renderers method."""

    # pylint: disable=W0246
    def get_renderers(self) -> list[BaseRenderer]:
        """Get renderers."""
        return super().get_renderers()


class WithDynamicViewSetMixin(
    HasInitializeRequest, HasGetRenderers, HasRequestProperty
):
    """A ViewSet that can support dynamic API features.

    Attributes:
      features: A list of features supported by the ViewSet.
      meta: Extra data that is added to the response by the DynamicRenderer.
    """

    DEBUG = "debug"
    SIDELOADING = "sideloading"
    PATCH_ALL = "patch-all"
    INCLUDE = "include[]"
    EXCLUDE = "exclude[]"
    FILTER = "filter{}"
    SORT = "sort[]"
    PAGE = settings.PAGE_QUERY_PARAM
    PER_PAGE = settings.PAGE_SIZE_QUERY_PARAM

    # TODO: add support for `sort{}`
    pagination_class = DynamicPageNumberPagination
    metadata_class = DynamicMetadata
    features = (
        DEBUG,
        INCLUDE,
        EXCLUDE,
        FILTER,
        PAGE,
        PER_PAGE,
        SORT,
        SIDELOADING,
        PATCH_ALL,
    )
    meta = None
    filter_backends = (DynamicFilterBackend, DynamicSortingFilter)

    def initialize_request(self, request: Request, *args, **kwargs) -> Request:
        """Initialize the request object.

        Override DRF initialize_request() method to swap request.GET
        (which is aliased by request.query_params) with a mutable instance
        of QueryParams, and to convert request MergeDict to a subclass of dict
        for consistency (MergeDict is not a subclass of dict)

        Args:
            request: Request object.
            *args: Args.
            **kwargs: Kwargs.

        Returns:
            Request: Request object.
        """
        request.GET = handle_encodings(request)
        return super().initialize_request(request, *args, **kwargs)

    def get_renderers(self) -> list[BaseRenderer]:
        """Optionally block Browsable API rendering.

        Returns:
            list[BaseRenderer]: List of renderers.
        """
        renderers = super().get_renderers()
        if settings.ENABLE_BROWSABLE_API is False:
            return [r for r in renderers if not isinstance(r, BrowsableAPIRenderer)]
        return renderers

    def get_request_feature(self, name, raw: bool = False):
        """Parses the request for a particular feature.

        Arguments:
          name: A feature name.
          raw: bool -

        Returns:
          A feature parsed from the URL if the feature is supported, or None.
        """
        name_is_feature = name in self.features
        request = self.request
        if "[]" in name:
            logger.debug("Using array-type feature: %s", name)
            return request.query_params.getlist(name) if name_is_feature else None
        elif "{}" in name:
            logger.debug(
                "Using object-type feature (keys are not consistent): %s", name
            )
            return (
                _extract_object_params(request, name, raw=raw)
                if name_is_feature
                else {}
            )
        logger.debug("Using single-type feature: %s", name)
        return request.query_params.get(name) if name_is_feature else None

    def get_queryset(self, queryset=None):  # pylint: disable=unused-argument
        """Returns a queryset for this request.

        Arguments:
          queryset: Optional root-level queryset.
        """
        serializer = self.get_serializer()
        return getattr(self, "queryset", serializer.Meta.model.objects.all())

    def get_request_fields(self):
        """Parses the INCLUDE and EXCLUDE features.

        Extracts the dynamic field features from the request parameters
        into a field map that can be passed to a serializer.

        Returns:
          A nested dict mapping serializer keys to
          True (include) or False (exclude).
        """
        if hasattr(self, "_request_fields"):
            return self._request_fields

        include_fields = self.get_request_feature(self.INCLUDE)
        exclude_fields = self.get_request_feature(self.EXCLUDE)
        request_fields = {}
        for fields, include in ((include_fields, True), (exclude_fields, False)):
            if fields is None:
                continue
            for field in fields:
                field_segments = field.split(".")
                num_segments = len(field_segments)
                current_fields = request_fields
                for i, segment in enumerate(field_segments):
                    last = i == num_segments - 1
                    if segment:
                        if last:
                            current_fields[segment] = include
                        else:
                            if segment not in current_fields:
                                current_fields[segment] = {}
                            current_fields = current_fields[segment]
                    elif not last:
                        # empty segment must be the last segment
                        raise exceptions.ParseError(f'"{field}" is not a valid field.')

        self._request_fields = request_fields
        return request_fields

    def get_request_patch_all(self):
        """Get request patch-all value."""
        patch_all = self.get_request_feature(self.PATCH_ALL)
        if not patch_all:
            return None
        patch_all = patch_all.lower()
        if patch_all == "query":
            pass
        elif is_truthy(patch_all):
            patch_all = True
        else:
            raise exceptions.ParseError(
                f'"{patch_all}" is not valid for {self.PATCH_ALL}'
            )
        return patch_all

    def get_request_debug(self):
        """Get request debug value."""
        debug = self.get_request_feature(self.DEBUG)
        return is_truthy(debug) if debug is not None else None

    def get_request_sideloading(self):
        """Get request sideloading value."""
        sideloading = self.get_request_feature(self.SIDELOADING)
        return is_truthy(sideloading) if sideloading is not None else None

    def is_update(self):
        """Return True if the request is an update request."""
        return self.request and self.request.method.upper() in UPDATE_REQUEST_METHODS

    def is_delete(self):
        """Return True if the request is a delete request."""
        return self.request and self.request.method.upper() == DELETE_REQUEST_METHOD

    def get_serializer(self, *args, **kwargs):
        """Return a serializer instance."""
        if "request_fields" not in kwargs:
            kwargs["request_fields"] = self.get_request_fields()
        if "sideloading" not in kwargs:
            kwargs["sideloading"] = self.get_request_sideloading()
        if "debug" not in kwargs:
            kwargs["debug"] = self.get_request_debug()
        if "envelope" not in kwargs:
            kwargs["envelope"] = True
        if self.is_update():
            kwargs["include_fields"] = "*"
        return super().get_serializer(*args, **kwargs)

    def paginate_queryset(self, *args, **kwargs):
        """Paginate the queryset if pagination is enabled."""
        if self.PAGE not in self.features:
            return
        query_params = self.request.query_params
        per_page = self.PER_PAGE
        # make sure pagination is enabled
        if per_page not in self.features and per_page in query_params:
            # remove per_page if it is disabled
            query_params[per_page] = None
        return super().paginate_queryset(*args, **kwargs)

    def _prefix_inex_params(self, request, feature, prefix):
        """Prefix include/exclude params with field_name."""
        values = self.get_request_feature(feature)
        if not values:
            return
        del request.query_params[feature]
        request.query_params.add(feature, [prefix + val for val in values])

    def list_related(self, request, pk=None, field_name=None):
        """List related.

        Fetch related object(s), as if side-loaded (used to support
        link objects).

        This method gets mapped to `/<resource>/<pk>/<field_name>/` by
        DynamicRouter for all DynamicRelationField fields. Generally,
        this method probably shouldn't be overridden.

        An alternative implementation would be to generate reverse queries.
        For an exploration of that approach, see:
            https://gist.github.com/ryochiji/54687d675978c7d96503
        """
        # Explicitly disable support filtering. Applying filters to this
        # endpoint would require us to pass through side-load filters, which
        # can have unintended consequences when applied asynchronously.
        if self.get_request_feature(self.FILTER):
            raise ValidationError("Filtering is not enabled on relation endpoints.")

        # Prefix include/exclude filters with field_name, so it's scoped to
        # the parent object.
        field_prefix = field_name + "."
        self._prefix_inex_params(request, self.INCLUDE, field_prefix)
        self._prefix_inex_params(request, self.EXCLUDE, field_prefix)

        # Filter for parent object, include related field.
        self.request.query_params.add("filter{pk}", pk)
        self.request.query_params.add(self.INCLUDE, field_prefix)

        # Get serializer and field.
        serializer = self.get_serializer()
        field = serializer.fields.get(field_name)
        if field is None:
            raise ValidationError(f'Unknown field: "{field_name}".')

        # Query for root object, with related field prefetched
        queryset = self.get_queryset()
        queryset = self.filter_queryset(queryset)
        obj = queryset.first()

        if not obj:
            return Response("Not found", status=404)

        # Serialize the related data. Use the field's serializer to ensure
        # it's configured identically to the sideload case. One difference
        # is we need to set `envelope=True` to get the sideload-processor
        # applied.
        related_szr = field.get_serializer(envelope=True)
        try:
            # TODO(ryo): Probably should use field.get_attribute() but that
            #            seems to break a bunch of things. Investigate later.
            related_szr.instance = getattr(obj, field.source)
        except ObjectDoesNotExist:
            # See:
            # http://jsonapi.org/format/#fetching-relationships-responses-404
            # This is a case where the "link URL exists but the relationship
            # is empty" and therefore must return a 200.
            return Response({}, status=200)

        return Response(related_szr.data)

    def get_extra_filters(self, request):  # pylint: disable=unused-argument
        """Get extra filters.

        Override this method to enable addition of extra filters
         (i.e., a Q()) so custom filters can be added to the queryset without
         running into https://code.djangoproject.com/ticket/18437
         which, without this, would mean that filters added to the queryset
         after this is called may not behave as expected.
        """
        return None


class DynamicModelViewSet(WithDynamicViewSetMixin, viewsets.ModelViewSet):
    """A ModelViewSet that supports dynamic API features."""

    ENABLE_BULK_PARTIAL_CREATION = settings.ENABLE_BULK_PARTIAL_CREATION
    ENABLE_BULK_UPDATE = settings.ENABLE_BULK_UPDATE
    ENABLE_PATCH_ALL = settings.ENABLE_PATCH_ALL

    def _get_bulk_payload(self, request):
        """Get bulk payload from request."""
        plural_name = self.get_serializer_class().get_plural_name()
        data = request.data
        if isinstance(data, list):
            return data
        elif plural_name in data and len(data) == 1:
            return data[plural_name]
        return None

    def _bulk_update(self, data, partial=False):
        """Bulk update records."""
        # Restrict the update to the filtered queryset.
        serializer = self.get_serializer(
            self.filter_queryset(self.get_queryset()),
            data=data,
            many=True,
            partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def _validate_patch_all(self, data):
        """Validate patch-all data."""
        if not isinstance(data, dict):
            raise ValidationError("Patch-all data must be in object form")
        serializer = self.get_serializer()
        fields = serializer.get_all_fields()
        validated = {}
        for name, value in data.items():
            field = fields.get(name, None)
            if field is None:
                raise ValidationError(f'Unknown field: "{name}"')
            source = field.source or name
            if source == "*" or field.read_only:
                raise ValidationError(f'Cannot update field: "{name}"')
            validated[source] = value
        return validated

    def _patch_all_query(self, queryset, data):
        """Update by queryset."""
        # update by queryset
        try:
            return queryset.update(**data)
        except Exception as e:
            raise ValidationError(
                "Failed to bulk-update records:\n" f"{str(e)}\n" f"Data: {str(data)}"
            ) from e

    def _patch_all_loop(self, queryset, data):
        """Update by transaction loop."""
        # update by transaction loop
        updated = 0
        try:
            with transaction.atomic():
                for record in queryset:
                    for k, v in data.items():
                        setattr(record, k, v)
                    record.save()
                    updated += 1
                return updated
        except IntegrityError as e:
            raise ValidationError(
                "Failed to update records:\n" f"{str(e)}\n" f"Data: {str(data)}"
            ) from e

    def _patch_all(self, data, query=False):
        """Update all records in a queryset."""
        queryset = self.filter_queryset(self.get_queryset())
        data = self._validate_patch_all(data)
        updated = (
            self._patch_all_query(queryset, data)
            if query
            else self._patch_all_loop(queryset, data)
        )
        return Response({"meta": {"updated": updated}}, status=status.HTTP_200_OK)

    def update(self, request, *args, **kwargs):
        """Update one or more model instances.

        If ENABLE_BULK_UPDATE is set, multiple previously-fetched records
        may be updated in a single call, provided their IDs.

        If ENABLE_PATCH_ALL is set, multiple records
        may be updated in a single PATCH call, even without knowing their IDs.

        *WARNING*: ENABLE_PATCH_ALL should be considered an advanced feature
        and used with caution. This feature must be enabled at the viewset level
        and must also be requested explicitly by the client
        via the "patch-all" query parameter.

        This parameter can have one of the following values:

            true (or 1): records will be fetched and then updated in a
             transaction loop
              - The `Model.save` method will be called and model signals
                will run
              - This can be slow if there are too many signals
               or many records in the query.
              - This is considered the more safe and default behavior
            query: records will be updated in a single query
              - The `QuerySet.update` method will be called and model
               signals will not run
              - This will be fast, but may break data constraints that
               are controlled by signals
              - This is considered unsafe but useful in certain situations

        The server's successful response to a patch-all request
        will NOT include any individual records.
        Instead, the response content will containa "meta" object
         with an "updated" count of updated records.

        Examples:

        Update one dog:

            PATCH /dogs/1/
            {
                'fur': 'white'
            }

        Update many dogs by ID:

            PATCH /dogs/
            [
                {'id': 1, 'fur': 'white'},
                {'id': 2, 'fur': 'black'},
                {'id': 3, 'fur': 'yellow'}
            ]

        Update all dogs in a query:

            PATCH /dogs/?filter{fur.contains}=brown&patch-all=true
            {
                'fur': 'gold'
            }
        """  # noqa
        if self.ENABLE_BULK_UPDATE:
            patch_all = self.get_request_patch_all()
            if self.ENABLE_PATCH_ALL and patch_all:
                # patch-all update
                data = request.data
                return self._patch_all(data, query=patch_all == "query")
            else:
                # bulk payload update
                partial = "partial" in kwargs
                bulk_payload = self._get_bulk_payload(request)
                if bulk_payload:
                    return self._bulk_update(bulk_payload, partial)

        # singular update
        try:
            return super().update(request, *args, **kwargs)
        except AssertionError as e:
            err = str(e)
            if "Fix your URL conf" in err:
                # this error is returned by DRF if a client
                # makes an update request (PUT or PATCH) without an ID
                # since DREST supports bulk updates with IDs contained in data,
                # we return a 400 instead of a 500 for this case,
                # as this is not considered a misconfiguration
                raise exceptions.ValidationError(err)
            else:
                raise

    def _create_many(self, data):
        """Create many model instances in bulk."""
        items = []
        errors = []
        result = {}
        serializers = []

        for entry in data:
            serializer = self.get_serializer(data=entry)
            try:
                serializer.is_valid(raise_exception=True)
            except exceptions.ValidationError as e:
                errors.append({"detail": str(e), "source": entry})
            else:
                if self.ENABLE_BULK_PARTIAL_CREATION:
                    self.perform_create(serializer)
                    items.append(serializer.to_representation(serializer.instance))
                else:
                    serializers.append(serializer)
        if not self.ENABLE_BULK_PARTIAL_CREATION and not errors:
            for serializer in serializers:
                self.perform_create(serializer)
                items.append(serializer.to_representation(serializer.instance))

        # Populate serialized data to the result.
        result = SideloadingProcessor(self.get_serializer(), items).data

        # Include errors if any.
        if errors:
            result["errors"] = errors

        code = status.HTTP_201_CREATED if not errors else status.HTTP_400_BAD_REQUEST

        return Response(result, status=code)

    def create(self, request, *args, **kwargs):
        """Create one or more model instances.

        Either create a single or many model instances in bulk
        using the Serializer's many=True ability from Django REST >= 2.2.5.

        The data can be represented by the serializer name (single or plural
        forms), dict or list.

        Examples:
        POST /dogs/
        {
          "name": "Fido",
          "age": 2
        }

        POST /dogs/
        {
          "dog": {
            "name": "Lucky",
            "age": 3
          }
        }

        POST /dogs/
        {
          "dogs": [
            {"name": "Fido", "age": 2},
            {"name": "Lucky", "age": 3}
          ]
        }

        POST /dogs/
        [
            {"name": "Fido", "age": 2},
            {"name": "Lucky", "age": 3}
        ]
        """
        bulk_payload = self._get_bulk_payload(request)
        if bulk_payload:
            return self._create_many(bulk_payload)
        return super().create(request, *args, **kwargs)

    def _destroy_many(self, data):
        """Destroy many model instances in bulk."""
        instances = (
            self.get_queryset().filter(id__in=[d["id"] for d in data]).distinct()
        )
        for instance in instances:
            self.check_object_permissions(self.request, instance)
            self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def destroy(self, request, *args, **kwargs):
        """Either delete a single or many model instances in bulk.

        DELETE /dogs/
        {
            "dogs": [
                {"id": 1},
                {"id": 2}
            ]
        }

        DELETE /dogs/
        [
            {"id": 1},
            {"id": 2}
        ]
        """
        bulk_payload = self._get_bulk_payload(request)
        if bulk_payload:
            return self._destroy_many(bulk_payload)
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        if lookup_url_kwarg not in kwargs:
            # assume that it is a poorly formatted bulk request
            return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
        return super().destroy(request, *args, **kwargs)
