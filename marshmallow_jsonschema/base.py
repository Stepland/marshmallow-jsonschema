import datetime
import decimal
import uuid
from inspect import isclass

from marshmallow import fields, missing, Schema, validate
from marshmallow.class_registry import get_class
from marshmallow.decorators import post_dump

from .compat import (
    text_type,
    binary_type,
    basestring,
    dot_data_backwards_compatible,
    list_inner,
    INCLUDE,
    EXCLUDE,
    RAISE,
)
from .exceptions import UnsupportedValueError
from .validation import handle_length, handle_one_of, handle_range

__all__ = ("JSONSchema",)

TYPE_MAP = {
    dict: {"type": "object"},
    list: {"type": "array"},
    datetime.time: {"type": "string", "format": "time"},
    datetime.timedelta: {
        # TODO explore using 'range'?
        "type": "string"
    },
    datetime.datetime: {"type": "string", "format": "date-time"},
    datetime.date: {"type": "string", "format": "date"},
    uuid.UUID: {"type": "string", "format": "uuid"},
    text_type: {"type": "string"},
    binary_type: {"type": "string"},
    decimal.Decimal: {"type": "number", "format": "decimal"},
    set: {"type": "array"},
    tuple: {"type": "array"},
    float: {"type": "number", "format": "float"},
    int: {"type": "number", "format": "integer"},
    bool: {"type": "boolean"},
}


FIELD_VALIDATORS = {
    validate.Length: handle_length,
    validate.OneOf: handle_one_of,
    validate.Range: handle_range,
}


def _resolve_additional_properties(cls):
    meta = cls.Meta

    additional_properties = getattr(meta, "additional_properties", None)
    if additional_properties is not None:
        if additional_properties in (True, False):
            return additional_properties
        else:
            raise UnsupportedValueError(
                "`additional_properties` must be either True or False"
            )

    unknown = getattr(meta, "unknown", None)
    if unknown is None:
        return False
    elif unknown in (RAISE, EXCLUDE):
        return False
    elif unknown == INCLUDE:
        return True
    else:
        raise UnsupportedValueError("Unknown value %s for `unknown`" % unknown)


class JSONSchema(Schema):
    """Converts to JSONSchema as defined by http://json-schema.org/."""

    properties = fields.Method("get_properties")
    type = fields.Constant("object")
    required = fields.Method("get_required")

    def __init__(self, *args, **kwargs):
        """Setup internal cache of nested fields, to prevent recursion."""
        self._nested_schema_classes = {}
        self.nested = kwargs.pop("nested", False)
        super(JSONSchema, self).__init__(*args, **kwargs)

    def _get_default_mapping(self, obj):
        """Return default mapping if there are no special needs."""
        mapping = {v: k for k, v in obj.TYPE_MAPPING.items()}
        mapping.update(
            {
                fields.Email: text_type,
                fields.Dict: dict,
                fields.Url: text_type,
                fields.List: list,
                fields.DateTime: datetime.datetime,
                fields.Nested: "_from_nested_schema",
                fields.Number: decimal.Decimal,
            }
        )
        return mapping

    def get_properties(self, obj):
        """Fill out properties field."""
        properties = {}

        for field_name, field in sorted(obj.fields.items()):
            schema = self._get_schema_for_field(obj, field)
            properties[field.name] = schema

        return properties

    def get_required(self, obj):
        """Fill out required field."""
        required = []

        for field_name, field in sorted(obj.fields.items()):
            if field.required:
                required.append(field.name)

        return required or missing

    def _from_python_type(self, obj, field, pytype):
        """Get schema definition from python type."""
        json_schema = {"title": field.attribute or field.name}

        for key, val in TYPE_MAP[pytype].items():
            json_schema[key] = val

        if field.dump_only:
            json_schema["readonly"] = True

        if field.default is not missing:
            json_schema["default"] = field.default

        # NOTE: doubled up to maintain backwards compatibility
        metadata = field.metadata.get("metadata", {})
        metadata.update(field.metadata)

        for md_key, md_val in metadata.items():
            if md_key == "metadata":
                continue
            json_schema[md_key] = md_val

        if isinstance(field, fields.List):
            json_schema["items"] = self._get_schema_for_field(obj, list_inner(field))
        return json_schema

    def _get_pytype(self, field, mapping):
        """Get pytype based on field subclass"""
        for map_class, pytype in mapping.items():
            if issubclass(field.__class__, map_class):
                return pytype
        raise UnsupportedValueError("unsupported field type %s" % field)

    def _get_schema_for_field(self, obj, field):
        """Get schema and validators for field."""
        mapping = self._get_default_mapping(obj)
        if hasattr(field, "_jsonschema_type_mapping"):
            schema = field._jsonschema_type_mapping()
        elif "_jsonschema_type_mapping" in field.metadata:
            schema = field.metadata["_jsonschema_type_mapping"]
        else:
            pytype = self._get_pytype(field, mapping)
            if isinstance(pytype, basestring):
                schema = getattr(self, pytype)(obj, field)
            else:
                schema = self._from_python_type(obj, field, pytype)
        # Apply any and all validators that field may have
        for validator in field.validators:
            if validator.__class__ in FIELD_VALIDATORS:
                schema = FIELD_VALIDATORS[validator.__class__](
                    schema, field, validator, obj
                )
        return schema

    def _from_nested_schema(self, obj, field):
        """Support nested field."""
        if isinstance(field.nested, basestring):
            nested = get_class(field.nested)
        else:
            nested = field.nested

        if isclass(nested) and issubclass(nested, Schema):
            name = nested.__name__
            only = field.only
            exclude = field.exclude
            nested_cls = nested
            nested_instance = nested(only=only, exclude=exclude)
        else:
            nested_cls = nested.__class__
            name = nested_cls.__name__
            nested_instance = nested

        outer_name = obj.__class__.__name__
        # If this is not a schema we've seen, and it's not this schema (checking this for recursive schemas),
        # put it in our list of schema defs
        if name not in self._nested_schema_classes and name != outer_name:
            wrapped_nested = self.__class__(nested=True)
            wrapped_dumped = dot_data_backwards_compatible(
                wrapped_nested.dump(nested_instance)
            )

            wrapped_dumped["additionalProperties"] = _resolve_additional_properties(
                nested_cls
            )

            self._nested_schema_classes[name] = wrapped_dumped

            self._nested_schema_classes.update(wrapped_nested._nested_schema_classes)

        # and the schema is just a reference to the def
        schema = {"type": "object", "$ref": "#/definitions/{}".format(name)}

        # NOTE: doubled up to maintain backwards compatibility
        metadata = field.metadata.get("metadata", {})
        metadata.update(field.metadata)

        for md_key, md_val in metadata.items():
            if md_key == "metadata":
                continue
            schema[md_key] = md_val

        if field.many:
            schema = {
                "type": "array" if field.required else ["array", "null"],
                "items": schema,
            }

        return schema

    def dump(self, obj, **kwargs):
        """Take obj for later use: using class name to namespace definition."""
        self.obj = obj
        return super(JSONSchema, self).dump(obj, **kwargs)

    @post_dump
    def wrap(self, data, **_):
        """Wrap this with the root schema definitions."""
        if self.nested:  # no need to wrap, will be in outer defs
            return data

        cls = self.obj.__class__
        name = cls.__name__

        data["additionalProperties"] = _resolve_additional_properties(cls)

        self._nested_schema_classes[name] = data
        root = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "definitions": self._nested_schema_classes,
            "$ref": "#/definitions/{name}".format(name=name),
        }
        return root
