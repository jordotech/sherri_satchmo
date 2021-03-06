from decimal import Decimal
from django.contrib.sites.models import Site
from django.db.models import Q
from livesettings import config_value
from l10n.utils import moneyfmt
from product.models import Option, ProductPriceLookup, OptionGroup, Discount, Product, split_option_unique_id
from satchmo_utils.numbers import round_decimal
import datetime
import logging
import types
import string

log = logging.getLogger('product.utils')

def calc_discounted_by_percentage(price, percentage):
    if not percentage:
        return price
    if percentage > 1:
        log.warn("Correcting discount percentage, should be less than 1, is %s", percentage)
        percentage = percentage/100

    work = price * (1-percentage)
    cents = Decimal("0.01")
    return work.quantize(cents)

def find_auto_discounts(product):
    if not type(product) in (types.ListType, types.TupleType):
        product = (product,)
    today = datetime.date.today()
    discs = Discount.objects.filter(automatic=True, active=True, startDate__lte=today, endDate__gt=today)
    return discs.filter(Q(valid_products__in=product) | Q(allValid=True)).order_by('-percentage')

def find_best_auto_discount(product):
    discs = find_auto_discounts(product)
    if len(discs) > 0:
        return discs[0]
    else:
        return None

def productvariation_details(product, include_tax, user, create=False):
    """Build the product variation details, for conversion to javascript.

    Returns variation detail dictionary built like so:
    details = {
        "OPTION_KEY" : {
            "SLUG": "Variation Slug",
            "PRICE" : {"qty" : "$price", [...]},
            "SALE" : {"qty" : "$price", [...]},
            "TAXED" : "$taxed price",   # omitted if no taxed price requested
            "QTY" : 1
        },
        [...]
    }
    """

    ignore_stock = config_value('PRODUCT','NO_STOCK_CHECKOUT')
    discount = find_best_auto_discount(product)
    use_discount = discount and discount.percentage > 0

    if include_tax:
        from tax.utils import get_tax_processor
        taxer = get_tax_processor(user=user)
        tax_class = product.taxClass

    details = {'SALE' : use_discount}

    variations = ProductPriceLookup.objects.filter(parentid=product.id).order_by("-price")
    if variations.count() == 0:
        if create:
            log.debug('Creating price lookup for %s', product)
            ProductPriceLookup.objects.smart_create_for_product(product)
            variations = ProductPriceLookup.objects.filter(parentid=product.id).order_by("-price")
        else:
            log.warning('You must run satchmo_rebuild_pricing and add it to a cron-job to run every day, or else the product details will not work for product detail pages.')
    for detl in variations:
        key = detl.key
        if details.has_key(key):
            detail = details[key]
            qty = detl.quantity
        else:
            detail = {}
            detail['SLUG'] = detl.productslug

            if not detl.active:
                qty = round_decimal('-1.0')
            elif ignore_stock:
                qty = round_decimal('10000.0')
            else:
                qty = round_decimal(detl.items_in_stock)

            detail['QTY'] = round_decimal(qty)

            detail['PRICE'] = {}

            if use_discount:
                detail['SALE'] = {}

            if include_tax:
                detail['TAXED'] = {}
                if use_discount:
                    detail['TAXED_SALE'] = {}

            if detl.productimage_set:
                    detail['ADDITIONAL_IMAGES'] = [u"%s" % prodimg.picture for prodimg in detl.productimage_set.all()]

            details[key] = detail

        qtykey = "%d" % detl.quantity

        price = detl.dynamic_price

        detail['PRICE'][qtykey] = moneyfmt(price)
        if use_discount:
            detail['SALE'][qtykey] = moneyfmt(calc_discounted_by_percentage(price, discount.percentage))

        if include_tax:
            tax_price = taxer.by_price(tax_class, price) + price
            detail['TAXED'][qtykey] = moneyfmt(tax_price)
            if use_discount:
                detail['TAXED_SALE'][qtykey] = moneyfmt(calc_discounted_by_percentage(tax_price, discount.percentage))

    return details

def rebuild_pricing():
    site = Site.objects.get_current()
    for lookup in ProductPriceLookup.objects.filter(siteid=site.id):
        lookup.delete()

    products = Product.objects.active_by_site(site=site, variations=False)

    productct = products.count()
    pricect = 0

    for product in products:
        prices = ProductPriceLookup.objects.smart_create_for_product(product)
        pricect += len(prices)

    return productct, pricect

def serialize_options(product, selected_options=()):
    """
    Return a list of optiongroups and options for display to the customer.
    Only returns options that are actually used by members of this product.

    Return Value:
    [
    {
    name: 'group name',
    id: 'group id',
    items: [{
        name: 'opt name',
        value: 'opt value',
        price_change: 'opt price',
        selected: False,
        },{..}]
    },
    {..}
    ]

    Note: This doesn't handle the case where you have multiple options and
    some combinations aren't available. For example, you have option_groups
    color and size, and you have a yellow/large, a yellow/small, and a
    white/small, but you have no white/large - the customer will still see
    the options white and large.
    """
    all_options = product.get_valid_options()
    group_sortmap = OptionGroup.objects.get_sortmap()

    # first get all objects
    # right now we only have a list of option.unique_ids, and there are
    # probably a lot of dupes, so first list them uniquely
    values = []

    if all_options != [[]]:
        vals = {}
        groups = {}
        opts = {}
        serialized = {}

        for options in all_options:
            for option in options:
                if not opts.has_key(option):
                    k, v = split_option_unique_id(option)
                    vals[v] = False
                    groups[k] = False
                    opts[option] = None

        for option in Option.objects.filter(option_group__id__in = groups.keys(), value__in = vals.keys()):
            uid = option.unique_id
            if opts.has_key(uid):
                opts[uid] = option

        # now we have all the objects in our "opts" dictionary, so build the serialization dict

        for option in opts.values():
            if not serialized.has_key(option.option_group_id):
                serialized[option.option_group.id] = {
                    'name': option.option_group.translated_name(),
                    'description': option.option_group.translated_description(),
                    'id': option.option_group.id,
                    'items': [],
                }
            if not option in serialized[option.option_group_id]['items']:
                serialized[option.option_group_id]['items'] += [option]
                option.selected = option.unique_id in selected_options

        # first sort the option groups
        for k, v in serialized.items():
            values.append((group_sortmap[k], v))

        if values:
            values.sort()
            values = zip(*values)[1]

        #now go back and make sure option items are sorted properly.
        for v in values:
            v['items'] = _sort_options(v['items'])

    log.debug('Serialized Options %s: %s', product.product.slug, values)
    return values

def _sort_options(lst):
    work = [(opt.sort_order, opt) for opt in lst]
    work.sort()
    return zip(*work)[1]

# All the functions below are used to validate custom attributes
# associated with a product or category.
# Custom ones can be added to the list via the admin setting ATTRIBUTE_VALIDATION


def validation_simple(value, obj=None):
    """
    Validates that at least one character has been entered.
    Not change is made to the value.
    """
    if len(value) >= 1:
        return True, value
    else:
        return False, value

def validation_integer(value, obj=None):
    """
   Validates that value is an integer number.
   No change is made to the value
    """
    try:
        check = int(value)
        return True, value
    except:
        return False, value

def validation_yesno(value, obj=None):
    """
    Validates that yes or no is entered.
    Converts the yes or no to capitalized version
    """
    if string.upper(value) in ["YES","NO"]:
        return True, string.capitalize(value)
    else:
        return False, value

def validation_decimal(value, obj=None):
    """
    Validates that the number can be converted to a decimal
    """
    try:
        check = Decimal(value)
        return True, value
    except:
        return False, value

def import_validator(validator):
    try:
        import_name, function_name = validator.rsplit('.', 1)
    except ValueError:
        # no dot; treat it as a global
        func = globals().get(validator, None)
        if not func:
            # we use ImportError to keep error handling for callers simple
            raise ImportError
        return validator
    else:
        # The below __import__() call is from python docs, and is equivalent to:
        #
        #   from import_name import function_name
        #
        import_module = __import__(import_name, globals(), locals(), [function_name])

        return getattr(import_module, function_name)

def validate_attribute_value(attribute, value, obj):
    """
    Helper function for forms that wish to validation a value for an
    AttributeOption.
    """
    return import_validator(attribute.validation)(value, obj)
