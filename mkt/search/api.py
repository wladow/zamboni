import json

from django.conf.urls import url

from tastypie import http
from tastypie.authorization import ReadOnlyAuthorization
from tastypie.throttle import BaseThrottle
from tastypie.utils import trailing_slash
from tower import ugettext as _

import amo
from access import acl

import mkt
from mkt.api.authentication import (SharedSecretAuthentication,
                                    OptionalOAuthAuthentication)
from mkt.api.base import CORSResource, http_error, MarketplaceResource
from mkt.api.resources import AppResource
from mkt.constants.features import FeatureProfile
from mkt.search.views import _filter_search, _get_query
from mkt.search.forms import ApiSearchForm
from mkt.webapps.models import Webapp
from mkt.webapps.utils import es_app_to_dict, update_with_reviewer_data


class SearchResource(CORSResource, MarketplaceResource):

    class Meta(AppResource.Meta):
        resource_name = 'search'
        allowed_methods = []
        detail_allowed_methods = []
        list_allowed_methods = ['get']
        authorization = ReadOnlyAuthorization()
        authentication = (SharedSecretAuthentication(),
                          OptionalOAuthAuthentication())
        slug_lookup = None
        # Override CacheThrottle with a no-op.
        throttle = BaseThrottle()

    def get_resource_uri(self, bundle):
        # Link to the AppResource URI.
        return AppResource().get_resource_uri(bundle.obj)

    def search_form(self, request):
        form = ApiSearchForm(request.GET if request else None)
        if not form.is_valid():
            raise self.form_errors(form)
        return form.cleaned_data

    def get_list(self, request=None, **kwargs):
        form_data = self.search_form(request)
        is_admin = acl.action_allowed(request, 'Admin', '%')
        is_reviewer = acl.action_allowed(request, 'Apps', 'Review')

        # Pluck out status and addon type first since it forms part of the base
        # query, but only for privileged users.
        status = form_data['status']
        addon_type = form_data['type']

        base_filters = {
            'type': addon_type,
        }

        # Allow reviewers and admins to search by statuses other than PUBLIC.
        if status and (status == 'any' or status != amo.STATUS_PUBLIC):
            if is_admin or is_reviewer:
                base_filters['status'] = status
            else:
                raise http_error(http.HttpUnauthorized,
                                 _('Unauthorized to filter by status.'))

        # Only allow reviewers and admin to search by private fields or fields
        # depending on the latest_version (which may or may not be public yet).
        restricted_data = [form_data.get('is_privileged', None),
                           form_data.get('has_editor_comment', None),
                           form_data.get('has_info_request', None),
                           form_data.get('is_escalated', None)]

        if not (is_admin or is_reviewer) and any(f is not None
                                                 for f in restricted_data):
            return http.HttpUnauthorized(content=json.dumps(
                {'reason': _('Unauthorized to filter by private fields.')}))

        # Filter by device feature profile.
        profile = None
        if request.GET.get('dev') in ('firefoxos', 'android'):
            sig = request.GET.get('pro')
            if sig:
                profile = FeatureProfile.from_signature(sig)

        # Filter by region.
        region = getattr(request, 'REGION', mkt.regions.WORLDWIDE)

        qs = _get_query(request, region, gaia=request.GAIA,
                        mobile=request.MOBILE, tablet=request.TABLET,
                        filters=base_filters, new_idx=True)
        qs = _filter_search(request, qs, form_data, region=region,
                            profile=profile)
        paginator = self._meta.paginator_class(request.GET, qs,
            resource_uri=self.get_resource_list_uri(),
            limit=self._meta.limit)
        page = paginator.page()

        # Rehydrate the results as per tastypie.
        objs = []
        for obj in page['objects']:
            obj.pk = obj.id
            objs.append(self.build_bundle(obj=obj, request=request))

        page['objects'] = [self.full_dehydrate(bundle) for bundle in objs]

        # This isn't as quite a full as a full TastyPie meta object,
        # but at least it's namespaced that way and ready to expand.
        to_be_serialized = self.alter_list_data_to_serialize(request, page)
        return self.create_response(request, to_be_serialized)

    def dehydrate(self, bundle):
        obj = bundle.obj
        amo_user = getattr(bundle.request, 'amo_user', None)

        bundle.data.update(es_app_to_dict(obj, region=bundle.request.REGION.id,
                                          profile=amo_user,
                                          request=bundle.request))

        # Add extra data for reviewers. Used in reviewer tool search.
        bundle = update_with_reviewer_data(bundle, using_es=True)

        return bundle

    def override_urls(self):
        return [
            url(r'^(?P<resource_name>%s)/featured%s$' %
                (self._meta.resource_name, trailing_slash()),
                self.wrap_view('with_featured'), name='api_with_featured')
            ]

    def with_featured(self, request, **kwargs):
        return WithFeaturedResource().dispatch('list', request, **kwargs)


class WithFeaturedResource(SearchResource):

    class Meta(SearchResource.Meta):
        authorization = ReadOnlyAuthorization()
        authentication = OptionalOAuthAuthentication()
        detail_allowed_methods = []
        fields = SearchResource.Meta.fields + ['id', 'cat']
        list_allowed_methods = ['get']
        resource_name = 'search/featured'
        slug_lookup = None

    def alter_list_data_to_serialize(self, request, data):
        form_data = self.search_form(request)
        region = getattr(request, 'REGION', mkt.regions.WORLDWIDE)
        cat_slug = form_data.get('cat')
        if cat_slug:
            cat_slug = [cat_slug]

        # Filter by device feature profile.
        profile = None
        if request.GET.get('dev') in ('firefoxos', 'android'):
            sig = request.GET.get('pro')
            if sig:
                profile = FeatureProfile.from_signature(sig)

        qs = Webapp.featured(cat=cat_slug, region=region, profile=profile)

        bundles = [self.build_bundle(obj=obj, request=request) for obj in qs]
        data['featured'] = [AppResource().full_dehydrate(bundle)
                            for bundle in bundles]
        # Alter the _view_name so that statsd logs seperately from search.
        request._view_name = 'featured'
        return data
