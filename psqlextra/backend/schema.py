import copy

from typing import Any, List, Tuple

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db.models import Field, Model

from psqlextra.types import PostgresPartitioningMethod

from . import base_impl
from .side_effects import (
    HStoreRequiredSchemaEditorSideEffect,
    HStoreUniqueSchemaEditorSideEffect,
)


class PostgresSchemaEditor(base_impl.schema_editor()):
    """Schema editor that adds extra methods for
    PostgreSQL specific features and hooks into
    existing implementations to add side effects
    specific to PostgreSQL."""

    sql_partition_by = " PARTITION BY %s (%s)"
    sql_add_default_partition = "CREATE TABLE %s PARTITION OF %s DEFAULT"
    sql_add_range_partition = (
        "CREATE TABLE %s PARTITION OF %s FOR VALUES FROM (%s) TO (%s)"
    )
    sql_add_list_partition = (
        "CREATE TABLE %s PARTITION OF %s FOR VALUES IN (%s)"
    )

    side_effects = [
        HStoreUniqueSchemaEditorSideEffect(),
        HStoreRequiredSchemaEditorSideEffect(),
    ]

    def __init__(self, connection, collect_sql=False, atomic=True):
        super().__init__(connection, collect_sql, atomic)

        for side_effect in self.side_effects:
            side_effect.execute = self.execute
            side_effect.quote_name = self.quote_name

        self.deferred_sql = []

    def create_model(self, model: Model) -> None:
        """Creates a new model."""

        super().create_model(model)

        for side_effect in self.side_effects:
            side_effect.create_model(model)

    def create_partitioned_model(self, model: Model) -> None:
        """Creates a new partitioned model."""

        partitioning_method, partitioning_key = self._partitioning_properties_for_model(
            model
        )

        # get the sql statement that django creates for normal
        # table creations..
        sql, params = self._extract_sql(self.create_model, model)

        partitioning_key_sql = ", ".join(
            self.quote_name(field_name) for field_name in partitioning_key
        )

        # create a composite key that includes the partitioning key
        sql = sql.replace(" PRIMARY KEY", "")
        sql = sql[:-1] + ", PRIMARY KEY (%s, %s))" % (
            self.quote_name(model._meta.pk.name),
            partitioning_key_sql,
        )

        # extend the standard CREATE TABLE statement with
        # 'PARTITION BY ...'
        sql += self.sql_partition_by % (
            partitioning_method.upper(),
            partitioning_key_sql,
        )

        self.execute(sql, params)

    def add_range_partition(
        self, model: Model, name: str, from_values: Any, to_values: Any
    ) -> None:
        """Creates a new range partition for the specified partitioned model.

        Arguments:
            model:
                Partitioned model to create a partition for.

            name:
                Name to give to the new partition table.

            from_values:
                Start of the partitioning key range of
                values that need to be stored in this
                partition.

            to_values:
                End of the partitioning key range of
                values that need to be stored in this
                partition.
        """

        # asserts the model is a model set up for partitioning
        self._partitioning_properties_for_model(model)

        sql = self.sql_add_range_partition % (
            self.quote_name(name),
            self.quote_name(model._meta.db_table),
            "%s",
            "%s",
        )

        self.execute(sql, (from_values, to_values))

    def add_list_partition(
        self, model: Model, name: str, values: List[Any]
    ) -> None:
        """Creates a new list partition for the specified partitioned model.

        Arguments:
            model:
                Partitioned model to create a partition for.

            name:
                Name to give to the new partition.

            values:
                Partition key values that should be
                stored in this partition.
        """

        # asserts the model is a model set up for partitioning
        self._partitioning_properties_for_model(model)

        sql = self.sql_add_list_partition % (
            self.quote_name(name),
            self.quote_name(model._meta.db_table),
            ",".join(["%s" for _ in range(len(values))]),
        )

        self.execute(sql, values)

    def add_default_partition(self, model: Model, name: str) -> None:
        """Creates a new default partition for the specified partitioned model.

        A default partition is a partition where rows are
        routed to when no more specific partition is a match."""
        # asserts the model is a model set up for partitioning
        self._partitioning_properties_for_model(model)

        sql = self.sql_add_default_partition % (
            self.quote_name(name),
            self.quote_name(model._meta.db_table),
        )

        self.execute(sql)

    def delete_model(self, model: Model) -> None:
        """Drops/deletes an existing model."""

        for side_effect in self.side_effects:
            side_effect.delete_model(model)

        super().delete_model(model)

    def alter_db_table(
        self, model: Model, old_db_table: str, new_db_table: str
    ) -> None:
        """Alters a table/model."""

        super().alter_db_table(model, old_db_table, new_db_table)

        for side_effect in self.side_effects:
            side_effect.alter_db_table(model, old_db_table, new_db_table)

    def add_field(self, model: Model, field: Field) -> None:
        """Adds a new field to an exisiting model."""

        super().add_field(model, field)

        for side_effect in self.side_effects:
            side_effect.add_field(model, field)

    def remove_field(self, model: Model, field: Field) -> None:
        """Removes a field from an existing model."""

        for side_effect in self.side_effects:
            side_effect.remove_field(model, field)

        super().remove_field(model, field)

    def alter_field(
        self,
        model: Model,
        old_field: Field,
        new_field: Field,
        strict: bool = False,
    ) -> None:
        """Alters an existing field on an existing model."""

        super().alter_field(model, old_field, new_field, strict)

        for side_effect in self.side_effects:
            side_effect.alter_field(model, old_field, new_field, strict)

    def _extract_sql(self, method, *args):
        """Calls the specified method with the specified arguments
        and intercepts the SQL statement it WOULD execute.

        We use this to figure out the exact SQL statement
        Django would execute. We can then make a small modification
        and execute it ourselves."""

        original_execute_func = copy.deepcopy(self.execute)

        intercepted_args = []

        def _intercept(*args):
            intercepted_args.extend(args)

        self.execute = _intercept

        method(*args)

        self.execute = original_execute_func
        return intercepted_args

    @staticmethod
    def _partitioning_properties_for_model(
        model: Model
    ) -> Tuple[PostgresPartitioningMethod, Any]:
        """Gets the partitioning options for the specified model.

        Raises:
            ImproperlyConfigured:
                When the specified model is not set up
                for partitioning.
        """

        partitioning_method = getattr(model, "partitioning_method", None)
        partitioning_key = getattr(model, "partitioning_key", None)

        if not partitioning_method or not partitioning_key:
            raise ImproperlyConfigured(
                (
                    "Model '%s' is not properly configured to be partitioned."
                    " Set the `partitioning_method` and `partitioning_key` attributes."
                )
                % model.__name__
            )

        if partitioning_method not in PostgresPartitioningMethod:
            raise ImproperlyConfigured(
                (
                    "Model '%s' is not properly configured to be partitioned."
                    " '%s' is not a member of the PostgresPartitioningMethod enum."
                )
                % (model.__name__, partitioning_method)
            )

        if not isinstance(partitioning_key, list):
            raise ImproperlyConfigured(
                (
                    "Model '%s' is not properly configured to be partitioned."
                    " Partitioning key should be a list (of field names or values,"
                    " depending on the partitioning method)."
                )
                % model.__name__
            )

        try:
            for field_name in partitioning_key:
                model._meta.get_field(field_name)
        except FieldDoesNotExist:
            raise ImproperlyConfigured(
                (
                    "Model '%s' is not properly configured to be partitioned."
                    " Field in partitioning key '%s' is not a valid field on"
                    " the model."
                )
                % (model.__name__, partitioning_key)
            )

        return partitioning_method, partitioning_key
