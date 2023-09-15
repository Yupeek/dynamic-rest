"""Module containing Django meta helpers."""
from __future__ import annotations

from functools import lru_cache
from itertools import chain

from django.core.exceptions import FieldDoesNotExist
from django.db.models import ManyToOneRel  # tested in 1.9
from django.db.models import OneToOneRel  # tested in 1.9
from django.db.models import (
    Model,
    ForeignKey,
    ManyToManyField,
    ManyToManyRel,
    OneToOneField
)

from dynamic_rest.related import RelatedObject


def is_model_field(model, field_name):
    """Check whether a given field exists on a model.

    Arguments:
        model: a Django model
        field_name: the name of a field

    Returns:
        True if `field_name` exists on `model`, False otherwise.
    """
    try:
        get_model_field(model, field_name)
        return True
    except AttributeError:
        return False


@lru_cache()
def get_model_relationships(meta) -> dict:
    related_objs = (
        f for f in meta.get_fields()
        if (f.one_to_many or f.one_to_one)
        and f.auto_created and not f.concrete
    )
    related_m2m_objs = (
        f for f in meta.get_fields(include_hidden=True)
        if f.many_to_many and f.auto_created
    )
    return {
        o.get_accessor_name(): o
        for o in chain(related_objs, related_m2m_objs)
    }


@lru_cache()
def get_virtual_fields(meta) -> dict | None:
    if hasattr(meta, 'virtual_fields'):
        return {
            f.name: f
            for f in meta.virtual_fields
        }


def get_model_field(model: Model | None, field_name):
    """Return a field given a model and field name.

    Arguments:
        model: a Django model
        field_name: the name of a field

    Returns:
        A Django field if `field_name` is a valid field for `model`,
            None otherwise.
    """
    if not model:
        # TODO: This might be a bug.
        return None

    meta = model._meta
    try:
        return meta.get_field(field_name)
    except FieldDoesNotExist:
        meta._related_fields_cache = cache = getattr(
            meta,
            '_related_fields_cache',
            get_model_relationships(meta)
        )

        if field_name in cache:
            return cache[field_name]
        # check virtual fields (1.7)
        if virtual_fields := get_virtual_fields(meta):
            if field := virtual_fields.get(field_name):
                return field

        raise AttributeError(
            f'{field_name} is not a valid field for {model}'
        )


def get_model_field_and_type(model, field_name):
    field = get_model_field(model, field_name)

    # Django 1.7 (and 1.8?)
    if isinstance(field, RelatedObject):
        if isinstance(field.field, OneToOneField):
            return field, 'o2or'
        elif isinstance(field.field, ManyToManyField):
            return field, 'm2m'
        elif isinstance(field.field, ForeignKey):
            return field, 'm2o'
        else:
            raise RuntimeError("Unexpected field type")

    # Django 1.9
    type_map = [
        (OneToOneField, 'o2o'),
        (OneToOneRel, 'o2or'),  # is subclass of m2o so check first
        (ManyToManyField, 'm2m'),
        (ManyToOneRel, 'm2o'),
        (ManyToManyRel, 'm2m'),
        (ForeignKey, 'fk'),  # check last
    ]
    for cls, type_str in type_map:
        if isinstance(field, cls):
            return field, type_str,

    return field, '',


def is_field_remote(model, field_name):
    """Check whether a given model field is a remote field.

    A remote field is the inverse of a one-to-many or a
    many-to-many relationship.

    Arguments:
        model: a Django model
        field_name: the name of a field

    Returns:
        True if `field_name` is a remote field, False otherwise.
    """
    if not hasattr(model, '_meta'):
        # ephemeral model with no metaclass
        return False

    model_field = get_model_field(model, field_name)
    return isinstance(model_field, (ManyToManyField, RelatedObject))


def get_related_model(field):
    try:
        # django 1.8+
        return field.related_model
    except AttributeError:
        # django 1.7
        if hasattr(field, 'field'):
            return field.field.model
        elif hasattr(field, 'rel'):
            return field.rel.to
        elif field.__class__.__name__ == 'GenericForeignKey':
            return None
        else:
            raise


def reverse_m2m_field_name(m2m_field):
    try:
        # Django 1.9
        return m2m_field.remote_field.name
    except BaseException:
        # Django 1.7
        if hasattr(m2m_field, 'rel'):
            return m2m_field.rel.related_name
        elif hasattr(m2m_field, 'field'):
            return m2m_field.field.name
        elif m2m_field.__class__.__name__ == 'GenericForeignKey':
            return None
        else:
            raise


def reverse_o2o_field_name(o2or_field):
    try:
        # Django 1.9
        return o2or_field.remote_field.attname
    except BaseException:
        # Django 1.7
        return o2or_field.field.attname


def get_remote_model(field):
    try:
        # Django 1.9
        return field.remote_field.model
    except BaseException:
        # Django 1.7
        if hasattr(field, 'field'):
            return field.field.model
        elif hasattr(field, 'rel'):
            return field.rel.to
        elif field.__class__.__name__ == 'GenericForeignKey':
            return None
        else:
            raise


def get_model_table(model):
    try:
        return model._meta.db_table
    except BaseException:
        return None
