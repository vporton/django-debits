import logging
from composite_field import CompositeField
from django.db import models
from django.utils.translation import ugettext_lazy as _


logger = logging.getLogger('debits')
"""The logger used by Debits."""


class Period(CompositeField):
    """Period (for example of recurring payment or of a trial subscription).

    It may be expressed in days, weeks, months, or years."""
    UNIT_DAYS = 1
    UNIT_WEEKS = 2
    UNIT_MONTHS = 3
    UNIT_YEARS = 4

    period_choices = ((UNIT_DAYS, _("days")),  # different processors may support a part of it
                      (UNIT_WEEKS, _("weeks")),
                      (UNIT_MONTHS, _("months")),
                      (UNIT_YEARS, _("years")))
    """For Django `ChoiceField`."""

    unit = models.SmallIntegerField()
    """days, weeks, months, or years."""

    count = models.SmallIntegerField()
    """The number of the units"""

    def __init__(self, unit=None, count=None):
        super().__init__()
        if unit is not None:
            self['unit'].default = unit
        if count is not None:
            self['count'].default = count

