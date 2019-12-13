import re
from base64 import b64decode
from datetime import datetime, time
from decimal import Decimal
from uuid import UUID

from django.conf import settings
from django.db.backends.base.operations import BaseDatabaseOperations
from django.utils import timezone
from spanner.dbapi.parse_utils import DateStr, TimestampStr, escape_name


class DatabaseOperations(BaseDatabaseOperations):
    # Django's lookup names that require a different name in Spanner's
    # EXTRACT() function.
    # https://cloud.google.com/spanner/docs/functions-and-operators#extract
    extract_names = {
        'week_day': 'dayofweek',
        'iso_week': 'isoweek',
        'iso_year': 'isoyear',
    }

    def quote_name(self, name):
        return escape_name(name)

    def bulk_insert_sql(self, fields, placeholder_rows):
        placeholder_rows_sql = (", ".join(row) for row in placeholder_rows)
        values_sql = ", ".join("(%s)" % sql for sql in placeholder_rows_sql)
        return "VALUES " + values_sql

    def sql_flush(self, style, tables, sequences, allow_cascade=False):
        # Cloud Spanner doesn't support TRUNCATE so DELETE instead.
        # A dummy WHERE clause is required.
        if tables:
            delete_sql = '%s %s %%s' % (
                style.SQL_KEYWORD('DELETE'),
                style.SQL_KEYWORD('FROM'),
            )
            return [
                delete_sql % style.SQL_FIELD(self.quote_name(table))
                for table in tables
            ]
        else:
            return []

    def adapt_datefield_value(self, value):
        if value is None:
            return None
        return DateStr(str(value))

    def adapt_datetimefield_value(self, value):
        if value is None:
            return None
        # Expression values are adapted by the database.
        if hasattr(value, 'resolve_expression'):
            return value
        # Cloud Spanner doesn't support tz-aware datetimes
        if timezone.is_aware(value):
            if settings.USE_TZ:
                value = timezone.make_naive(value, self.connection.timezone)
            else:
                raise ValueError(
                    "The Cloud Spanner backend does not support "
                    "timezone-aware datetimes when USE_TZ is False."
                )
        return TimestampStr(value.isoformat(timespec='microseconds') + 'Z')

    def adapt_decimalfield_value(self, value, max_digits=None, decimal_places=None):
        """
        Convert value from decimal.Decimal into float, for a direct mapping
        and correct serialization with RPCs to Cloud Spanner.
        """
        if value is None:
            return None
        return float(value)

    def adapt_timefield_value(self, value):
        if value is None:
            return None
        # Column is TIMESTAMP, so prefix a dummy date to the datetime.time.
        return TimestampStr('0001-01-01T' + value.isoformat(timespec='microseconds') + 'Z')

    def get_db_converters(self, expression):
        converters = super().get_db_converters(expression)
        internal_type = expression.output_field.get_internal_type()
        if internal_type == 'DateTimeField':
            converters.append(self.convert_datetimefield_value)
        elif internal_type == 'DecimalField':
            converters.append(self.convert_decimalfield_value)
        elif internal_type == 'TimeField':
            converters.append(self.convert_timefield_value)
        elif internal_type == 'BinaryField':
            converters.append(self.convert_binaryfield_value)
        elif internal_type == 'UUIDField':
            converters.append(self.convert_uuidfield_value)
        return converters

    def convert_binaryfield_value(self, value, expression, connection):
        if value is None:
            return value
        # Cloud Spanner stores bytes base64 encoded.
        return b64decode(value)

    def convert_datetimefield_value(self, value, expression, connection):
        if value is None:
            return value
        # Cloud Spanner returns the
        # google.api_core.datetime_helpers.DatetimeWithNanoseconds subclass
        # of datetime with tzinfo=UTC (which should be replaced with the
        # connection's timezone). Django doesn't support nanoseconds so that
        # part is ignored.
        return datetime(
            value.year, value.month, value.day,
            value.hour, value.minute, value.second, value.microsecond,
            self.connection.timezone,
        )

    def convert_decimalfield_value(self, value, expression, connection):
        if value is None:
            return value
        # Cloud Spanner returns a float.
        return Decimal(str(value))

    def convert_timefield_value(self, value, expression, connection):
        if value is None:
            return value
        # Convert DatetimeWithNanoseconds to time.
        return time(value.hour, value.minute, value.second, value.microsecond)

    def convert_uuidfield_value(self, value, expression, connection):
        if value is not None:
            value = UUID(value)
        return value

    def date_extract_sql(self, lookup_type, field_name):
        lookup_type = self.extract_names.get(lookup_type, lookup_type)
        return 'EXTRACT(%s FROM %s)' % (lookup_type, field_name)

    def datetime_extract_sql(self, lookup_type, field_name, tzname):
        tzname = tzname if settings.USE_TZ else 'UTC'
        lookup_type = self.extract_names.get(lookup_type, lookup_type)
        return 'EXTRACT(%s FROM %s AT TIME ZONE "%s")' % (lookup_type, field_name, tzname)

    def time_extract_sql(self, lookup_type, field_name):
        # Time is stored as TIMESTAMP with UTC time zone.
        return 'EXTRACT(%s FROM %s AT TIME ZONE "UTC")' % (lookup_type, field_name)

    def date_trunc_sql(self, lookup_type, field_name):
        # https://cloud.google.com/spanner/docs/functions-and-operators#date_trunc
        if lookup_type == 'week':
            # Spanner truncates to Sunday but Django expects Monday. First,
            # subtract a day so that a Sunday will be truncated to the previous
            # week...
            field_name = 'DATE_SUB(' + field_name + ', INTERVAL 1 DAY)'
        sql = 'DATE_TRUNC(%s, %s)' % (field_name, lookup_type)
        if lookup_type == 'week':
            # ...then add a day to get from Sunday to Monday.
            sql = 'DATE_ADD(' + sql + ', INTERVAL 1 DAY)'
        return sql

    def datetime_trunc_sql(self, lookup_type, field_name, tzname):
        # https://cloud.google.com/spanner/docs/functions-and-operators#timestamp_trunc
        tzname = tzname if settings.USE_TZ else 'UTC'
        if lookup_type == 'week':
            # Spanner truncates to Sunday but Django expects Monday. First,
            # subtract a day so that a Sunday will be truncated to the previous
            # week...
            field_name = 'TIMESTAMP_SUB(' + field_name + ', INTERVAL 1 DAY)'
        sql = 'TIMESTAMP_TRUNC(%s, %s, "%s")' % (field_name, lookup_type, tzname)
        if lookup_type == 'week':
            # ...then add a day to get from Sunday to Monday.
            sql = 'TIMESTAMP_ADD(' + sql + ', INTERVAL 1 DAY)'
        return sql

    def lookup_cast(self, lookup_type, internal_type=None):
        # Cast text lookups to string to allow things like filter(x__contains=4)
        if lookup_type in ('contains', 'icontains', 'startswith', 'istartswith',
                           'endswith', 'iendswith', 'regex', 'iregex'):
            return 'CAST(%s AS STRING)'
        return '%s'

    def prep_for_like_query(self, x):
        """Lookups that use this method use REGEX_CONTAINS instead of LIKE."""
        return re.escape(str(x))

    prep_for_iexact_query = prep_for_like_query
