import abc
import hmac
import datetime
from dateutil.relativedelta import relativedelta
import logging
from django.apps import apps
from django.urls import reverse
from django.db import models
from django.db.models import F
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.translation import ugettext_lazy as _
from composite_field import CompositeField
from django.conf import settings


logger = logging.getLogger('debits')


class ModelRef(CompositeField):
    app_label = models.CharField(max_length=100)
    model = models.CharField(_('python model class name'), max_length=100)


# The following two functions does not work as methods, because
# CompositeField is replaced with composite_field.base.CompositeField.Proxy:

def model_from_ref(model_ref):
    return apps.get_model(model_ref.app_label, model_ref.model)


class PaymentProcessor(models.Model):
    name = models.CharField(max_length=255)
    url = models.URLField(max_length=255)
    api = ModelRef()

    def __str__(self):
        return self.name


class Product(models.Model):
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class Period(CompositeField):
    UNIT_DAYS = 1
    UNIT_WEEKS = 2
    UNIT_MONTHS = 3
    UNIT_YEARS = 4

    period_choices = ((UNIT_DAYS, _("days")),  # different processors may support a part of it
                      (UNIT_WEEKS, _("weeks")),
                      (UNIT_MONTHS, _("months")),
                      (UNIT_YEARS, _("years")))

    unit = models.SmallIntegerField()
    count = models.SmallIntegerField()

    def __init__(self, unit=None, count=None):
        super().__init__()
        if unit is not None:
            self['unit'].default = unit
        if count is not None:
            self['count'].default = count

# The following two functions does not work as methods, because
# CompositeField is replaced with composite_field.base.CompositeField.Proxy:

# See Monthly Billing Cycles in
# https://developer.paypal.com/docs/classic/paypal-payments-standard/integration-guide/subscription_billing_cycles/
# there's 4 datetime libraries for python: datetime, arrow, pendulum, delorean, python-dateutil
def period_to_delta(period):
    return {
        Period.UNIT_DAYS: lambda: relativedelta(days=period.count),
        Period.UNIT_WEEKS: lambda: relativedelta(weeks=period.count),
        Period.UNIT_MONTHS: lambda: relativedelta(months=period.count),
        Period.UNIT_YEARS: lambda: relativedelta(years=period.count),
    }[period.unit]()


def period_to_string(period):
    hash = {e[0]: e[1] for e in Period.period_choices}
    return "%d %s" % (period.count, hash[period.unit])


class BaseTransaction(models.Model):
    """
    ONE redirect to the payment processor
    """

    # class Meta:
    #     abstract = True

    processor = models.ForeignKey(PaymentProcessor)
    creation_date = models.DateField(auto_now_add=True)

    def __repr__(self):
        return "<BaseTransaction: %s>" % (("pk=%d" % self.pk) if self.pk else "no pk")

    @staticmethod
    def custom_from_pk(pk):
        # Secret can be known only to one who created a BaseTransaction.
        # This prevents third parties to make fake IPNs from a payment processor.
        secret = hmac.new(settings.SECRET_KEY.encode(), ('payid ' + str(pk)).encode()).hexdigest()
        return settings.PAYMENTS_REALM + ' ' + str(pk) + ' ' + secret

    @staticmethod
    def pk_from_custom(custom):
        r = custom.split(' ', 2)
        if len(r) != 3 or r[0] != settings.PAYMENTS_REALM:
            raise BaseTransaction.DoesNotExist
        try:
            pk = int(r[1])
            secret = hmac.new(settings.SECRET_KEY.encode(), ('payid ' + str(pk)).encode()).hexdigest()
            if r[2] != secret:
                raise BaseTransaction.DoesNotExist
            return pk
        except ValueError:
            raise BaseTransaction.DoesNotExist

    # https://bitbucket.org/arcamens/django-payments/wiki/Invoice%20IDs
    @abc.abstractmethod
    def invoice_id(self):
        pass

    def invoiced_item(self):
        return self.item.old_subscription.basetransaction.item \
            if self.item and self.item.old_subscription \
            else self.item

    @abc.abstractmethod
    def subinvoice(self):
        pass

class SimpleTransaction(BaseTransaction):
    item = models.ForeignKey('SimpleItem', related_name='transactions', null=False)

    def subinvoice(self):
        return 1

    def invoice_id(self):
        return settings.PAYMENTS_REALM + ' p-%d' % (self.item.pk,)

class SubscriptionTransaction(BaseTransaction):
    item = models.ForeignKey('SubscriptionItem', related_name='transactions', null=False)

    def subinvoice(self):
        return self.invoiced_item().subinvoice

    def invoice_id(self):
        if self.item.old_subscription:
            return settings.PAYMENTS_REALM + ' %d-%d-u' % (self.item.pk, self.subinvoice())
        else:
            return settings.PAYMENTS_REALM + ' %d-%d' % (self.item.pk, self.subinvoice())


class Item(models.Model):
    """
    Apps using this package should create
    their product records manually.

    I may provide an interface for registering
    new products.
    """
    creation_date = models.DateField(auto_now_add=True)

    product = models.ForeignKey('Product', null=True)
    product_qty = models.IntegerField(default=1)
    blocked = models.BooleanField(default=False)  # hacker or misbehavior detected

    currency = models.CharField(max_length=3, default='USD')
    price = models.DecimalField(max_digits=10, decimal_places=2)  # for recurring payee the amount of one payment
    shipping = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # code = models.CharField(max_length=255) # TODO
    gratis = models.BooleanField(default=False)  # provide a product or service for free
    # recurring = models.BooleanField(default=False)

    # 0 - no reminder sent
    # 1 - before due payment sent
    # 2 - at due payment sent
    # 3 - day before deadline sent
    reminders_sent = models.SmallIntegerField(default=0, db_index=True)

    # We remove old_subscription automatically when new subscription is created.
    # The new payment may be either one-time (SimpleItem) or subscription (SubscriptionItem).
    old_subscription = models.ForeignKey('Subscription', null=True, related_name='new_subscription')

    def __repr__(self):
        return "<Item pk=%d, %s>" % (self.pk, self.product.name)

    def __str__(self):
        return self.product.name

    def adjust(self):
        pass

    @abc.abstractmethod
    def is_subscription(self):
        pass

    def send_rendered_email(self, template_name, subject, data):
        try:
            self.email = self.subscription.email
        except AttributeError:
            return
        if self.email is None:  # hack!
            return
        self.save()
        text = render_to_string(template_name, data, request=None, using=None)
        # FIXME: second argument should be plain text
        send_mail(subject, text, settings.FROM_EMAIL, [self.email], html_message=text)

class SimpleItem(Item):
    """
    Non-subscription item.
    """

    paid = models.BooleanField(default=False)

    def is_subscription(self):
        return False

    def is_paid(self):
        return (self.paid or self.gratis) and not self.blocked


class SubscriptionItem(Item):
    item = models.OneToOneField(Item, related_name='subscriptionitem', parent_link=True)

    active_subscription = models.OneToOneField('Subscription', null=True)

    due_payment_date = models.DateField(default=datetime.date.today, db_index=True)
    payment_deadline = models.DateField(null=True, db_index=True)  # may include "grace period"
    last_payment = models.DateField(null=True, db_index=True)

    trial = models.BooleanField(default=False, db_index=True)  # now in trial

    grace_period = Period(unit=Period.UNIT_DAYS, count=20)
    payment_period = Period(unit=Period.UNIT_MONTHS, count=1)
    trial_period = Period(unit=Period.UNIT_MONTHS, count=0)

    # https://bitbucket.org/arcamens/django-payments/wiki/Invoice%20IDs
    subinvoice = models.PositiveIntegerField(default=1)  # no need for index, as it is used only at PayPal side

    def is_subscription(self):
        return True

    # Usually you should use quick_is_active() instead because that is faster
    def is_active(self):
        prior = self.payment_deadline is not None and \
                datetime.date.today() <= self.payment_deadline
        return (prior or self.gratis) and not self.blocked

    @staticmethod
    def quick_is_active(item_id):
        transaction = SubscriptionItem.objects.filter(pk=item_id).\
            only('payment_deadline', 'gratis', 'blocked').get()
        return transaction.is_active()

    @staticmethod
    def day_needs_adjustment(period, date):
        return (period.unit == Period.UNIT_MONTHS and date.day >= 29) or \
                (period.unit == Period.UNIT_YEARS and \
                             date.month == 2 and date.day == 29)

    def adjust(self):
        self.trial = self.trial_period.count != 0
        self.adjust_dates()
        self.save()

    # If one bills at 29, 30, or 31, he should be given additional about 1-3 days free
    def adjust_dates(self):
        # We may have a trouble with non-monthly trials - the only solution is to make trial period ourselves
        creation_date = self.creation_date if self.creation_date else datetime.date.today()  # for not yet saved records
        period_end = creation_date + period_to_delta(self.trial_period)
        if self.due_payment_date:
            period_end = max(period_end, self.due_payment_date)
        if SubscriptionItem.day_needs_adjustment(self.trial_period, period_end):
            self.do_adjust_dates(period_end)

    def do_adjust_dates(self, period_end):
        period = period_end - datetime.date.today()
        while period_end.day != 1:
            period_end += datetime.timedelta(days=1)
            period += datetime.timedelta(days=1)
        # self.trial_period.both = (Period.UNIT_DAYS, period.days)  # setting it to due payment date would be wrong
        self.set_payment_date(period_end)

    def set_payment_date(self, date):
        self.due_payment_date = date
        self.payment_deadline = self.due_payment_date + period_to_delta(self.grace_period)

    def start_trial(self):
        self.trial = True
        self.set_payment_date(datetime.date.today() + period_to_delta(self.trial_period))

    def cancel_subscription(self):
        # atomic operation
        SubscriptionItem.objects.filter(pk=self.pk).update(active_subscription=None,
                                                           subinvoice=F('subinvoice') + 1)
        if not self.old_subscription:  # don't send this email on plan upgrade
            self.cancel_subscription_email()

    def cancel_subscription_email(self):
        url = settings.PAYMENTS_HOST + reverse(settings.PROLONG_PAYMENT_VIEW, args=[self.pk])
        days_before = (self.due_payment_date - datetime.date.today()).days
        self.send_rendered_email('payee/email/subscription-canceled.html',
                                 _("Service subscription canceled"),
                                 {'self': self,
                                  'product': self.product.name,
                                  'url': url,
                                  'days_before': days_before})

    @staticmethod
    def send_reminders():
        SubscriptionItem.send_regular_reminders()
        SubscriptionItem.send_trial_reminders()

    @staticmethod
    def send_regular_reminders():
        # start with the last
        SubscriptionItem.send_regular_before_due_reminders()
        SubscriptionItem.send_regular_due_reminders()
        SubscriptionItem.send_regular_deadline_reminders()

    @staticmethod
    def send_regular_before_due_reminders():
        days_before = settings.PAYMENTS_DAYS_BEFORE_DUE_REMIND
        reminder_date = datetime.date.today() + datetime.timedelta(days=days_before)
        q = SubscriptionItem.objects.filter(reminders_sent__lt=3, due_payment_date__lte=reminder_date, trial=False)
        for transaction in q:
            transaction.reminders_set = 3
            transaction.save()
            url = reverse(settings.PROLONG_PAYMENT_VIEW, args=[transaction.pk])
            transaction.send_rendered_email('payee/email/before-due-remind.html',
                                            _("You need to pay for %s") % transaction.product.name,
                                            {'transaction': transaction,
                                             'product': transaction.product.name,
                                             'url': url,
                                             'days_before': days_before})

    @staticmethod
    def send_regular_due_reminders():
        reminder_date = datetime.date.today()
        q = SubscriptionItem.objects.filter(reminders_sent__lt=2, due_payment_date__lte=reminder_date, trial=False)
        for transaction in q:
            transaction.reminders_set = 2
            transaction.save()
            url = reverse(settings.PROLONG_PAYMENT_VIEW, args=[transaction.pk])
            transaction.send_rendered_email('payee/email/due-remind.html',
                                            _("You need to pay for %s") % transaction.product.name,
                                            {'transaction': transaction,
                                             'product': transaction.product.name,
                                             'url': url})

    @staticmethod
    def send_regular_deadline_reminders():
        reminder_date = datetime.date.today()
        q = SubscriptionItem.objects.filter(reminders_sent__lt=1, payment_deadline__lte=reminder_date, trial=False)
        for transaction in q:
            transaction.reminders_set = 1
            transaction.save()
            url = reverse(settings.PROLONG_PAYMENT_VIEW, args=[transaction.pk])
            transaction.send_rendered_email('payee/email/deadline-remind.html',
                                            _("You need to pay for %s") % transaction.product.name,
                                            {'transaction': transaction,
                                             'product': transaction.product.name,
                                             'url': url})

    @staticmethod
    def send_trial_reminders():
        # start with the last
        SubscriptionItem.send_trial_before_due_reminders()
        SubscriptionItem.send_trial_due_reminders()
        SubscriptionItem.send_trial_deadline_reminders()

    @staticmethod
    def send_trial_before_due_reminders():
        days_before = settings.PAYMENTS_DAYS_BEFORE_TRIAL_END_REMIND
        reminder_date = datetime.date.today() + datetime.timedelta(days=days_before)
        q = SubscriptionItem.objects.filter(reminders_sent__lt=3, due_payment_date__lte=reminder_date, trial=True)
        for transaction in q:
            transaction.reminders_set = 3
            transaction.save()
            url = reverse(settings.PROLONG_PAYMENT_VIEW, args=[transaction.pk])
            transaction.send_rendered_email('payee/email/before-due-remind.html',
                                            _("You need to pay for %s") % transaction.product.name,
                                            {'transaction': transaction,
                                             'product': transaction.product.name,
                                             'url': url,
                                             'days_before': days_before})

    @staticmethod
    def send_trial_due_reminders():
        reminder_date = datetime.date.today()
        q = SubscriptionItem.objects.filter(reminders_sent__lt=2, due_payment_date__lte=reminder_date, trial=True)
        for transaction in q:
            transaction.reminders_set = 2
            transaction.save()
            url = reverse(settings.PROLONG_PAYMENT_VIEW, args=[transaction.pk])
            transaction.send_rendered_email('payee/email/due-remind.html',
                                            _("You need to pay for %s") % transaction.product.name,
                                            {'transaction': transaction,
                                             'product': transaction.product.name,
                                             'url': url})

    @staticmethod
    def send_trial_deadline_reminders():
        reminder_date = datetime.date.today()
        q = SubscriptionItem.objects.filter(reminders_sent__lt=1, payment_deadline__lte=reminder_date, trial=True)
        for transaction in q:
            transaction.reminders_set = 1
            transaction.save()
            url = reverse(settings.PROLONG_PAYMENT_VIEW, args=[transaction.pk])
            transaction.send_rendered_email('payee/email/deadline-remind.html',
                                            _("You need to pay for %s") % transaction.product.name,
                                            {'transaction': transaction,
                                             'product': transaction.product.name,
                                             'url': url})


class ProlongItem(Item):
    item = models.OneToOneField(Item, related_name='prolongitem', parent_link=True)
    parent = models.ForeignKey('SubscriptionItem', related_name='child', parent_link=False)
    prolong = Period(unit=Period.UNIT_MONTHS, count=0)  # TODO: rename

    def refund_payment(self):
        self.parent.set_payment_date(self.parent.due_payment_date - period_to_delta(self.prolong))
        self.parent.save()


class Subscription(models.Model):
    """
    When the user subscribes for automatic payment.
    """

    transaction = models.OneToOneField('BaseTransaction')

    # Avangate has it for every product, but PayPal for transaction as a whole.
    # So have it both in AutomaticPayment and Subscription
    subscription_reference = models.CharField(max_length=255, null=True)  # as recurring_payment_id in PayPal

    # duplicates email in Payment
    email = models.EmailField(null=True)  # DalPay requires to notify the customer 10 days before every payment

    def force_cancel(self, is_upgrade=False):
        if self.subscription_reference:
            klass = model_from_ref(self.subscriptiontransaction.processor.api)
            api = klass()
            api.cancel_agreement(self.subscription_reference, is_upgrade=is_upgrade)  # may raise an exception
            # transaction.cancel_subscription()  # runs in the callback


class Payment(models.Model):
    # The transaction which corresponds to the starting
    # process of purchase.
    transaction = models.OneToOneField('BaseTransaction')
    email = models.EmailField(null=True)  # DalPay requires to notify the customer 10 days before every payment

    def refund_payment(self):
        try:
            self.manualsubscriptionpayment.refund_payment()
        except ObjectDoesNotExist:
            pass


# FIXME: Store it in DB as a separate (non-proxy) model?
class AutomaticPayment(Payment):
    """
    This class models automatic payment.
    """

    pass

    # subscription = models.ForeignKey('Subscription')

    # curr = models.CharField(max_length=3, default='USD')

    # A transaction should have a code that identifies it.
    # code = models.CharField(max_length=255)


class CannotCancelSubscription(Exception):
    pass
