import datetime
import httplib2
import itertools
import json

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum, Max

from apiclient.discovery import build
import commonware.log
from celeryutils import task
from oauth2client.client import OAuth2Credentials

import amo
import amo.search
from addons.models import Addon, AddonUser
from bandwagon.models import Collection
from mkt.monolith.models import MonolithRecord
from mkt.webapps.models import Installed
from stats.models import Contribution
from reviews.models import Review
from users.models import UserProfile
from versions.models import Version
from lib.es.utils import get_indices
from .models import (AddonCollectionCount, CollectionCount, CollectionStats,
                     DownloadCount, ThemeUserCount, UpdateCount)
from . import search


log = commonware.log.getLogger('z.task')


@task
def addon_total_contributions(*addons, **kw):
    "Updates the total contributions for a given addon."

    log.info('[%s@%s] Updating total contributions.' %
             (len(addons), addon_total_contributions.rate_limit))
    # Only count uuid=None; those are verified transactions.
    stats = (Contribution.objects.filter(addon__in=addons, uuid=None)
             .values_list('addon').annotate(Sum('amount')))

    for addon, total in stats:
        Addon.objects.filter(id=addon).update(total_contributions=total)


@task
def update_addons_collections_downloads(data, **kw):
    log.info("[%s] Updating addons+collections download totals." %
                  (len(data)))
    cursor = connection.cursor()
    q = ("UPDATE addons_collections SET downloads=%s WHERE addon_id=%s "
         "AND collection_id=%s;" * len(data))
    cursor.execute(q,
                   list(itertools.chain.from_iterable(
                       [var['sum'], var['addon'], var['collection']]
                       for var in data)))
    transaction.commit_unless_managed()


@task
def update_collections_total(data, **kw):
    log.info("[%s] Updating collections' download totals." %
                   (len(data)))
    for var in data:
        (Collection.objects.filter(pk=var['collection_id'])
         .update(downloads=var['sum']))


def get_profile_id(service, domain):
    """
    Fetch the profile ID for the given domain.
    """
    accounts = service.management().accounts().list().execute()
    account_ids = [a['id'] for a in accounts.get('items', ())]
    for account_id in account_ids:
        webproperties = service.management().webproperties().list(
            accountId=account_id).execute()
        webproperty_ids = [p['id'] for p in webproperties.get('items', ())]
        for webproperty_id in webproperty_ids:
            profiles = service.management().profiles().list(
                accountId=account_id,
                webPropertyId=webproperty_id).execute()
            for p in profiles.get('items', ()):
                # sometimes GA includes "http://", sometimes it doesn't.
                if '://' in p['websiteUrl']:
                    name = p['websiteUrl'].partition('://')[-1]
                else:
                    name = p['websiteUrl']

                if name == domain:
                    return p['id']


@task
def update_google_analytics(date, **kw):
    creds_data = getattr(settings, 'GOOGLE_ANALYTICS_CREDENTIALS', None)
    if not creds_data:
        log.critical('Failed to update global stats: '
                     'GOOGLE_ANALYTICS_CREDENTIALS not set')
        return

    creds = OAuth2Credentials(
        *[creds_data[k] for k in
          ('access_token', 'client_id', 'client_secret',
           'refresh_token', 'token_expiry', 'token_uri',
           'user_agent')])
    h = httplib2.Http()
    creds.authorize(h)
    service = build('analytics', 'v3', http=h)
    domain = getattr(settings, 'GOOGLE_ANALYTICS_DOMAIN', None) or settings.DOMAIN
    profile_id = get_profile_id(service, domain)
    if profile_id is None:
        log.critical('Failed to update global stats: could not access a Google'
                     ' Analytics profile for ' + domain)
        return
    datestr = date.strftime('%Y-%m-%d')
    try:
        data = service.data().ga().get(ids='ga:' + profile_id,
                                       start_date=datestr,
                                       end_date=datestr,
                                       metrics='ga:visits').execute()
        # Storing this under the webtrends stat name so it goes on the
        # same graph as the old webtrends data.
        p = ['webtrends_DailyVisitors', data['rows'][0][0], date]
    except Exception, e:
        log.critical(
            'Fetching stats data for %s from Google Analytics failed: %s' % e)
        return
    try:
        cursor = connection.cursor()
        cursor.execute('REPLACE INTO global_stats (name, count, date) '
                       'values (%s, %s, %s)', p)
        transaction.commit_unless_managed()
    except Exception, e:
        log.critical('Failed to update global stats: (%s): %s' % (p, e))
        return

    log.debug('Committed global stats details: (%s) has (%s) for (%s)'
              % tuple(p))


@task
def update_global_totals(job, date, **kw):
    log.info("Updating global statistics totals (%s) for (%s)" %
                   (job, date))

    jobs = _get_daily_jobs(date)
    jobs.update(_get_metrics_jobs(date))

    num = jobs[job]()

    q = """REPLACE INTO
                global_stats(`name`, `count`, `date`)
            VALUES
                (%s, %s, %s)"""
    p = [job, num or 0, date]

    try:
        cursor = connection.cursor()
        cursor.execute(q, p)
        transaction.commit_unless_managed()
    except Exception, e:
        log.critical("Failed to update global stats: (%s): %s" % (p, e))

    # monolith is only used for marketplace
    if job.startswith(('apps', 'mmo')):
        try:
            value = json.dumps({'count': num or 0})
            MonolithRecord(recorded=date, key=job, value=value,
                           user_hash='none').save()
        except Exception as e:
            log.critical("Update of monolith table failed: (%s): %s" % (p, e))

    log.debug("Committed global stats details: (%s) has (%s) for (%s)"
              % tuple(p))


def _get_daily_jobs(date=None):
    """Return a dictionary of statistics queries.

    If a date is specified and applies to the job it will be used.  Otherwise
    the date will default to today().
    """
    if not date:
        date = datetime.date.today()

    # Passing through a datetime would not generate an error,
    # but would pass and give incorrect values.
    if isinstance(date, datetime.datetime):
        raise ValueError('This requires a valid date, not a datetime')

    # Testing on lte created date doesn't get you todays date, you need to do
    # less than next date. That's because 2012-1-1 becomes 2012-1-1 00:00
    next_date = date + datetime.timedelta(days=1)

    date_str = date.strftime('%Y-%m-%d')
    extra = dict(where=['DATE(created)=%s'], params=[date_str])

    # If you're editing these, note that you are returning a function!  This
    # cheesy hackery was done so that we could pass the queries to celery
    # lazily and not hammer the db with a ton of these all at once.
    stats = {
        # Add-on Downloads
        'addon_total_downloads': lambda: DownloadCount.objects.filter(
                date__lt=next_date).aggregate(sum=Sum('count'))['sum'],
        'addon_downloads_new': lambda: DownloadCount.objects.filter(
                date=date).aggregate(sum=Sum('count'))['sum'],

        # Add-on counts
        'addon_count_new': Addon.objects.extra(**extra).count,

        # Version counts
        'version_count_new': Version.objects.extra(**extra).count,

        # User counts
        'user_count_total': UserProfile.objects.filter(
                created__lt=next_date).count,
        'user_count_new': UserProfile.objects.extra(**extra).count,

        # Review counts
        'review_count_total': Review.objects.filter(created__lte=date,
                                                    editorreview=0).count,
        'review_count_new': Review.objects.filter(editorreview=0).extra(
                **extra).count,

        # Collection counts
        'collection_count_total': Collection.objects.filter(
                created__lt=next_date).count,
        'collection_count_new': Collection.objects.extra(**extra).count,
        'collection_count_autopublishers': Collection.objects.filter(
                created__lt=next_date, type=amo.COLLECTION_SYNCHRONIZED).count,

        'collection_addon_downloads': (lambda:
            AddonCollectionCount.objects.filter(date__lte=date).aggregate(
                sum=Sum('count'))['sum']),

        # Marketplace stats
        'apps_count_new': (Addon.objects
                .filter(created__range=(date, next_date),
                        type=amo.ADDON_WEBAPP).count),
        'apps_count_installed': (Installed.objects
                .filter(created__range=(date, next_date),
                        addon__type=amo.ADDON_WEBAPP).count),

        # Marketplace reviews
        'apps_review_count_new': Review.objects
                .filter(created__range=(date, next_date),
                        editorreview=0, addon__type=amo.ADDON_WEBAPP).count,

        # New users
        'mmo_user_count_total': UserProfile.objects.filter(
                created__lt=next_date,
                source=amo.LOGIN_SOURCE_MMO_BROWSERID).count,
        'mmo_user_count_new': UserProfile.objects.filter(
                created__range=(date, next_date),
                source=amo.LOGIN_SOURCE_MMO_BROWSERID).count,

        # New developers
        'mmo_developer_count_total': AddonUser.objects.filter(
            addon__type=amo.ADDON_WEBAPP).values('user').distinct().count,
    }

    # If we're processing today's stats, we'll do some extras.  We don't do
    # these for re-processed stats because they change over time (eg. add-ons
    # move from sandbox -> public
    if date == datetime.date.today():
        stats.update({
            'addon_count_experimental': Addon.objects.filter(
                created__lte=date, status=amo.STATUS_UNREVIEWED,
                disabled_by_user=0).count,
            'addon_count_nominated': Addon.objects.filter(
                created__lte=date, status=amo.STATUS_NOMINATED,
                disabled_by_user=0).count,
            'addon_count_public': Addon.objects.filter(
                created__lte=date, status=amo.STATUS_PUBLIC,
                disabled_by_user=0).count,
            'addon_count_pending': Version.objects.filter(
                created__lte=date, files__status=amo.STATUS_PENDING).count,

            'collection_count_private': Collection.objects.filter(
                created__lte=date, listed=0).count,
            'collection_count_public': Collection.objects.filter(
                created__lte=date, listed=1).count,
            'collection_count_editorspicks': Collection.objects.filter(
                created__lte=date, type=amo.COLLECTION_FEATURED).count,
            'collection_count_normal': Collection.objects.filter(
                created__lte=date, type=amo.COLLECTION_NORMAL).count,
        })

    return stats


def _get_metrics_jobs(date=None):
    """Return a dictionary of statistics queries.

    If a date is specified and applies to the job it will be used.  Otherwise
    the date will default to the last date metrics put something in the db.
    """

    if not date:
        date = UpdateCount.objects.aggregate(max=Max('date'))['max']

    # If you're editing these, note that you are returning a function!
    stats = {
        'addon_total_updatepings': lambda: UpdateCount.objects.filter(
                date=date).aggregate(sum=Sum('count'))['sum'],
        'collector_updatepings': lambda: UpdateCount.objects.get(
                addon=11950, date=date).count,
    }

    return stats


@task
def index_update_counts(ids, **kw):
    index = kw.pop('index', None)
    indices = get_indices(index)

    es = amo.search.get_es()
    qs = UpdateCount.objects.filter(id__in=ids)
    if qs:
        log.info('Indexing %s updates for %s.' % (qs.count(), qs[0].date))
    try:
        for update in qs:
            key = '%s-%s' % (update.addon_id, update.date)
            data = search.extract_update_count(update)
            for index in indices:
                UpdateCount.index(data, bulk=True, id=key, index=index)
        es.flush_bulk(forced=True)
    except Exception, exc:
        index_update_counts.retry(args=[ids], exc=exc, **kw)
        raise


@task
def index_download_counts(ids, **kw):
    index = kw.pop('index', None)
    indices = get_indices(index)

    es = amo.search.get_es()
    qs = DownloadCount.objects.filter(id__in=ids)
    if qs:
        log.info('Indexing %s downloads for %s.' % (qs.count(), qs[0].date))
    try:
        for dl in qs:
            key = '%s-%s' % (dl.addon_id, dl.date)
            data = search.extract_download_count(dl)
            for index in indices:
                DownloadCount.index(data, bulk=True, id=key, index=index)

        es.flush_bulk(forced=True)
    except Exception, exc:
        index_download_counts.retry(args=[ids], exc=exc)
        raise


@task
def index_collection_counts(ids, **kw):
    index = kw.pop('index', None)
    indices = get_indices(index)

    es = amo.search.get_es()
    qs = CollectionCount.objects.filter(collection__in=ids)
    if qs:
        log.info('Indexing %s addon collection counts: %s'
                 % (qs.count(), qs[0].date))
    try:
        for collection_count in qs:
            collection = collection_count.collection_id
            key = '%s-%s' % (collection, collection_count.date)
            filters = dict(collection=collection,
                           date=collection_count.date)
            data = search.extract_addon_collection(
                collection_count,
                AddonCollectionCount.objects.filter(**filters),
                CollectionStats.objects.filter(**filters))
            for index in indices:
                CollectionCount.index(data, bulk=True, id=key, index=index)
        es.flush_bulk(forced=True)
    except Exception, exc:
        index_collection_counts.retry(args=[ids], exc=exc)
        raise


@task
def index_theme_user_counts(ids, **kw):
    index = kw.pop('index', None)
    indices = get_indices(index)

    es = amo.search.get_es()
    qs = ThemeUserCount.objects.filter(id__in=ids)

    if qs:
        log.info('Indexing %s theme user counts for %s.'
                 % (qs.count(), qs[0].date))
    try:
        for user_count in qs:
            key = '%s-%s' % (user_count.addon_id, user_count.date)
            data = search.extract_theme_user_count(user_count)
            for index in indices:
                ThemeUserCount.index(data, bulk=True, id=key, index=index)
            es.flush_bulk(forced=True)
    except Exception, exc:
        index_theme_user_counts.retry(args=[ids], exc=exc)
        raise
