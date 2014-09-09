from django import forms
from django.contrib import admin
from django.conf.urls import patterns, url
from django.http import HttpResponse
from django.shortcuts import render_to_response, redirect


import autocomplete_light
from models import *
from util import *
from tasks import *
from admin_actions import *
from oauth_managers import TwitterOAuthManager, FacebookOAuthManager
from dream import settings

from datetime import datetime

import uuid

from django.db.models.signals import pre_delete
from django.dispatch import receiver

import logging
logging.basicConfig(filename=None, format='%(asctime)-6s: %(name)s - %(levelname)s - %(module)s - %(funcName)s - %(lineno)d - %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel('DEBUG')

iconpath = "/media/static/"

### Receivers ###

@receiver(pre_delete, sender=Item)
def itemDeleteReceiver(sender, **kwargs):
    obj = kwargs.get('instance')
    if obj.merged_from is not None:
        for i in obj.merged_from.all():
            i.hide = False
            i.save()
# end itemDeleteReceiver

### Forms ###

class QueryEventForm(forms.ModelForm):
    class Meta:
        model = QueryEvent
    # end QueryEventForm.Meta class

    def clean_creator(self):
        if not self.cleaned_data['creator']:
            return User()
        return self.cleaned_data['creator']
    # end QueryEventForm.clean_creator

    def clean(self):
        cleaned_data = super(QueryEventForm, self).clean()
        searchtype = cleaned_data.get("search_by")

        if searchtype == 'ST':
            querystring = cleaned_data.get("querystring")
            if querystring is None:
                raise forms.ValidationError(
                    'Must select a QueryString to perform a string query.'  )
        elif searchtype == 'UR':
            user = cleaned_data.get("user")
            if user is None:
                raise forms.ValidationError(
                    'Must select a User to perform a user query.'   )
        elif searchtype == 'TG':
            tag = cleaned_data.get("tag")
            if tag is None:
                raise forms.ValidationError(
                    'Must select a Tag to perform tag query.'   )
        return cleaned_data
    # end QueryEventForm.clean
# end QueryEventForm class



### Inlines ###

class QueryEventInline(admin.TabularInline):
    model = QueryEvent
    readonly_fields = ('dispatched', 'range', 'engine', 'datetime', 'results')
    exclude = ( 'rangeStart', 'rangeEnd', 'search_task', 'thumbnail_tasks', 
                'queryresults'  )
    ordering = ('datetime',)

    extra = 0
    def has_delete_permission(self, request, obj=None):
        """
        :class:`.QueryEvent` should not be deletable.
        """
        return False
    # end QueryEventInline.has_delete_permission

    def range(self, obj):
        """
        Prettier representation of the start and end indices.
        """
        pattern = u'{0}-{1}'
        return pattern.format(obj.rangeStart, obj.rangeEnd)
    range.allow_tags = True
    # end QueryEventInline.range

    def results(self, obj):
        """
        Yields the number of :class:`.Item` associated with this
        :class:`.QueryEvent`\, with a link to the filtered admin list view for
        :class:`.Item`\.
        """

        items = Item.objects.filter(events__id=obj.id)
        if len(items) > 0:
            pattern = u'<a href="{0}?events__id__exact={1}">{2} items</a>'
            baseurl = '/'.join(get_admin_url(items[0]).split('/')[0:-2])

            return pattern.format(baseurl, obj.id, len(items))
        return None
    results.allow_tags = True
    # end QueryEventInline.results
# end QueryEventInline class

### ModelAdmins ###

class QueryStringAdmin(admin.ModelAdmin):
    list_display = ('querystring', 'events', 'last_used')#, 'items')
    inlines = (QueryEventInline,)

    def get_urls(self):
        urls = super(QueryStringAdmin, self).get_urls()
        my_urls = patterns('',
            (r'^distribution/$', self.admin_site.admin_view(self.engine_matrix))
        )
        return my_urls + urls
    # end QueryStringAdmin.get_urls

    def engine_matrix(self, request):
        """
        should be able to see a matrix of querystrings versus engines.
        """

        querystrings = { q.id:q.querystring for q in QueryString.objects.all() }

        engines = { e.id:unicode(e) for e in Engine.objects.all() }

        values = { q:{ g:0 for g in engines.keys() } 
                        for q in querystrings.keys() }
        events = QueryEvent.objects.all()
        for e in events:
            if e.search_by == 'ST':
                items = Item.objects.filter(events__id=e.id).exclude(hide=True)
                q = e.querystring.id
                g = e.engine.id

                values[q][g] += len(items)

        pattern = "{0}admin/dolon/item/?events__engine__id__exact={1}&events__querystring__id__exact={2}"
        
        values_ = [ (querystrings[k], [
                        (pattern.format(settings.APP_DIR, g,k), vals[g], g, k ) 
                            for g in engines
                    ]) for k,vals in values.iteritems() ]

        context = {
            'title': 'Distribution of items across search terms and search engines',
            'values': values_,
            'engines': engines.values(),
            'iconpath': iconpath,
        }

        return render_to_response('querystring_matrix.html',context)
    # end QueryStringAdmin.engine_matrix

    def last_used(self, obj):
        print obj.latest()
        return pretty_date(obj.latest())
    # end QueryStringAdmin.last_used

    def get_readonly_fields(self, request, obj=None):
        """
        Value of ``querystring`` should not be editable after creation.
        """

        if obj:
            return ('querystring',) + self.readonly_fields
        return self.readonly_fields
    # end QueryStringAdmin.get_readonly_fields

    def get_inline_instances(self, request, obj=None):
        """
        Should only display related :class:`.QueryEvent` instances when editing.
        """

        if obj:
            return super(QueryStringAdmin, self).get_inline_instances(request, obj)
        return []
    # end QueryStringAdmin.get_inline_instances
# end QueryStringAdmin class

class QueryEventAdmin(admin.ModelAdmin):
    form = QueryEventForm

    list_display = ('id', 'query', 'engine', 'created', 'range',
                    'dispatched', 'search_status', 'results')
    list_display_links = ('query',)
    actions = [dispatch, reset]

    fieldsets = (
            (None, {
                'fields': ('search_by','engine')
            }),
            ('Search by string', {
                'classes': ('collapse',),
                'fields': ('querystring', 'rangeStart', 'rangeEnd'),
            }),
            ('Search by user', {
                'classes': ('collapse',),
                'fields': ('user',),
            }),
            ('Search by tag', {
                'classes': ('collapse',),
                'fields': ('tag',),
            }),
        )


    def query(self, obj):
        if obj.search_by == 'ST':
            param = obj.querystring.querystring
            method = 'String'
        elif obj.search_by == 'UR':
            param = '{0} ({1})'.format(obj.user.handle, obj.user.platform.name)
            method = 'User'
        elif obj.search_by == 'TG':
            param = obj.tag.string
            method = 'Tag'
        return '{0}: {1}'.format(method, param)
    # end QueryEventAdmin.query

    def created(self, obj):
        return obj.datetime
    # end QueryEventAdmin.created

    def result_sets(self, obj):
        """
        Generates a list of :class:`.QueryResult` instances associated with this
        :class:`.QueryEvent`\, with links to their respective admin change
        pages.
        """

        pattern = u'<a href="{0}">{1}, s:{2}, e:{3}</a>'
        R = [ pattern.format(get_admin_url(r), obj.querystring.querystring,
                 r.rangeStart, r.rangeEnd) for r in obj.queryresults.all() ]

        return u'\n'.join(R)
    result_sets.allow_tags = True
    # end QueryEventAdmin.result_sets

    def results(self, obj):
        """
        Yields the number of :class:`.Item` associated with this
        :class:`.QueryEvent`\, with a link to the filtered admin list view for
        :class:`.Item`\.
        """

        items = Item.objects.filter(events__id=obj.id).exclude(hide=True)
        if len(items) > 0:
            pattern = u'<a href="{0}?events__id__exact={1}">{2} items</a>'
            baseurl = u'/'.join(get_admin_url(items[0]).split('/')[0:-2])

            return pattern.format(baseurl, obj.id, len(items))
        return None
    results.allow_tags = True
    # end QueryEventAdmin.results

    def range(self, obj):
        """
        Prettier representation of the start and end indices.
        """

        pattern = u'{0}-{1}'
        return pattern.format(obj.rangeStart, obj.rangeEnd)
    range.allow_tags = True
    # end QueryEventAdmin.range

    def get_readonly_fields(self, request, obj=None):
        """
        All fields should be readonly after creation.
        """

        if obj:
            read_only = (
                'querystring', 'datetime', 'engine', 'range', 'dispatched',
                'results', 'search_status', 'creator', 'rangeStart',
                'rangeEnd' ) + self.readonly_fields
            return read_only
        return self.readonly_fields
    # end QueryEventAdmin.get_readonly_fields

    def get_form(self, request, obj=None, **kwargs):
        """
        Should not display :class:`.QueryResult` when adding.
        """

        exclude = [ 'search_task', 'thumbnail_tasks', 'queryresults', 'state' ]
        print obj, self.exclude
        if obj is None:
            self.exclude = exclude + ['dispatched', 'creator']

        else:
            pass
        form = super(QueryEventAdmin, self).get_form(request, obj, **kwargs)

        # List for initial form values in GET request.
        if request.method == 'GET':
            for key in request.GET:
                try:
                    form.__dict__[key].initial = request.GET[key]
                except KeyError:    # Unexpected parameter; ignore.
                    pass

        return form
    # end QueryEventAdmin.get_form

    def save_model(self, request, obj, form, change):

        if not hasattr(obj, 'creator'):
            obj.creator = request.user
        obj.save()
    # end QueryEventAdmin.save_model
# end QueryEventAdmin class

class ItemAdmin(admin.ModelAdmin):
    form = autocomplete_light.modelform_factory(Item)
    list_display = ('icon', 'list_preview','title', 'status','retrieved', 'type' )
    readonly_fields = ( 'item_preview', 'contents', 'creator', 'resource',
                        'status', 'retrieved', 'type', 'query_events',
                        'contexts', 'creationDate',  'children', 'parent',  )
    exclude = ( 'image', 'thumbnail', 'events', 'merged_with', 'url',
                'hide', 'context'  )
    list_filter = ('status','events__querystring', 'events__engine', 'tags', 'type','events')
    list_editable = ['title',]
    list_select_related = True
    search_fields = ['title',]
    list_per_page = 5

    actions = [ approve, reject, pend, merge, retrieve_content ]

    def save_model(self, request, obj, form, change):
        """
        On save, should also updated the target of ``merged_with``.

        Updates:
        * Contexts
        * Tags
        """
        obj.save()
        if obj.merged_with is not None:
            'ok'
            for c in request.POST.getlist('context'):
                obj.merged_with.context.add(Context.objects.get(pk=int(c)))

            for t in request.POST.getlist('tags'):
                obj.merged_with.tags.add(Tag.objects.get(pk=int(t)))

            obj.merged_with.save()
    # end ItemAdmin.save_model

    def queryset(self, request):
        """
        Filter the queryset to exclude hidden items.
        """

        qs = super(ItemAdmin, self).queryset(request)
        if request.path.split('/')[-2] == 'item':   # Only filter changelist.
            return qs.exclude(hide=True)
        return qs
    # end ItemAdmin.queryset

    ## Custom fields...
    def parent(self, obj):
        """
        Display the item into which this item has been merged.
        """

        pattern = u'<a href="{0}">{1}</a>'
        if obj.merged_with is not None:
            href = get_admin_url(obj.merged_with)
            title = obj.merged_with.title
            return pattern.format(href, title)
        return None
    parent.allow_tags = True
    # end ItemAdmin.parent

    def children(self, obj):
        """
        Display merged items from whence this item originated.
        """

        pattern = u'<li><a href="{0}">{1}</a></li>'

        html = u'<ul>'
        for c in obj.merged_from.all():
            html += pattern.format(get_admin_url(c), c.title)
        html += u'</ul>'
        return html
    children.allow_tags = True
    # end ItemAdmin.children

    def list_preview(self, obj, **kwargs):
        """
        Generates a thumbnail, or player.
        """

        return self._item_image(obj, True)
    list_preview.allow_tags = True
    # end ItemAdmin.list_preview

    def item_preview(self, obj, **kwargs):
        return self._item_image(obj, False)
    item_preview.allow_tags = True
    # end ItemAdmin.item_preview

    def resource(self, obj):
        """
        Generates a link to the original image URL, opening in a new tab.
        """

        pattern = u'<a href="{0}" target="_blank">{0}</a>'
        return pattern.format(obj.url)
    resource.allow_tags = True
    # end ItemAdmin.resource

    def contexts(self, obj):
        """
        Generates a list of associated :class:`.Context` instances, with links
        to their respective admin change pages.
        """

        pattern = u'<li><a href="{0}">{1}</a></li>'
        repr = u''.join([ pattern.format(get_admin_url(c),c.url)
                            for c in obj.context.all() ])
        return u'<ul>{0}</ul>'.format(repr)
    contexts.allow_tags = True
    # end ItemAdmin.contexts

    def icon(self, obj, list=False):
        """
        Display a media type icon.
        """

        return self._format_type_icon(obj.type)
    icon.allow_tags = True
    # end ItemAdmin.icon

    def contents(self, obj, list=True):
        """
        Display the content objects associated with an Item.
        """

        logger.debug(obj.type)
        logger.debug(obj.type == 'Text')

        pattern = u'<a href="{0}">{1}</a>'

        if obj.type == 'Audio':
            logger.debug('Display contents of AudioItem.')
            formatted = []
            for seg in obj.audioitem.audio_segments.all():
                icon = self._format_mime_icon(seg.type(), 'audio')
                _url = get_admin_url(seg)
                formatted.append(pattern.format(_url, icon))

            return u''.join(formatted)
        elif obj.type == 'Video':
            logger.debug('Display contents of VideoItem.')
            formatted = []
            for vid in obj.videoitem.videos.all():
                icon = self._format_mime_icon(vid.type(), 'video')
                _url = get_admin_url(vid)
                formatted.append(pattern.format(_url, icon))
            return u''.join(formatted)

        elif obj.type == 'Image':
            logger.debug('Display contents of ImageItem.')
            formatted = []
            for img in obj.imageitem.images.all():
                icon = self._format_mime_icon(img.type(), 'image')
                _url = get_admin_url(img)
                formatted.append(pattern.format(_url, icon))
            return u''.join(formatted)
        elif obj.type == 'Text':
            logger.debug('Display contents of TextItem.')
            formatted = []
            for txt in obj.textitem.original_files.all():
                icon = self._format_mime_icon(txt.mime, 'text')
                _url = get_admin_url(txt)
                formatted.append(pattern.format(_url, icon))
            return u''.join(formatted)
    contents.allow_tags = True
    # end ItemAdmin.contents

    def _format_mime_icon(self, mime, alt=None):
        """
        Get an icon according to mime type.
        """
        known_types = {
            'image/png':        '/dolon/media/static/png-by-Hopstarter.png',
            'image/jpeg':       '/dolon/media/static/jpeg-by-Hopstarter.png',
            'image/gif':        '/dolon/media/static/gif-by-Hopstarter.png',
            'image/tiff':       '/dolon/media/static/tiff-by-Hopstarter.png',
            'image/bmp':        '/dolon/media/static/bmp-by-Hopstarter.png',
            'audio/flac':       '/dolon/media/static/flac-by-Hopstarter.png',
            'audio/mpeg':       '/dolon/media/static/mp3-by-Hopstarter.png',
            'audio/wav':        '/dolon/media/static/wav-by-Hopstarter.png',
            'audio/aiff':       '/dolon/media/static/aiff-by-Hopstarter.png',
            'video/mpeg':       '/dolon/media/static/mpeg-by-Hopstarter.png',
            'video/avi':        '/dolon/media/static/avi-by-Hopstarter.png',
            'video/x-ms-wmv':   '/dolon/media/static/wmv-by-Hopstarter.png',
            'video/3gpp':       '/dolon/media/static/3gp-by-Hopstarter.png',
            'video/quicktime':  '/dolon/media/static/mov-by-Hopstarter.png',
        }

        alt_types = {
            'image':        '/dolon/media/static/jpeg-by-Hopstarter.png',
            'audio':        '/dolon/media/static/audio-by-Hopstarter.png',
            'video':        '/dolon/media/static/video-by-Hopstarter.png',
            'text':         '/dolon/media/static/text-by-Hopstarter.png',
        }

        pattern = u'<img src="{0}" height="{1}" />'
        if mime in known_types:
            icon_path = known_types[mime]
            return pattern.format(icon_path, 50)
        elif alt in alt_types:
            icon_path = alt_types[alt]
            return pattern.format(icon_path, 50)
        return None
    # end ItemAdmin._format_mime_icon

    def _format_type_icon(self, type):
        """
        Get an icon according to file type.
        """
        pattern = u'<img src="{0}" height="{1}" />'
        if type == 'Audio':
            iconpath = u'/dolon/media/static/audio-by-Hopstarter.png'
        elif type == 'Video':
            iconpath = u'/dolon/media/static/video-by-Hopstarter.png'
        elif type == 'Image':
            iconpath = u'/dolon/media/static/jpeg-by-Hopstarter.png'
        elif type == 'Text':
            iconpath = u'/dolon/media/static/text-by-Hopstarter.png'
        else:
            return None
        return pattern.format(iconpath, 50)
    # end ItemAdmin._format_type_icon

    def _format_thumb(self, obj, thumb, list):
        pattern = u'<a href="{0}"><img src="{1}"/></a>'
        if thumb is not None and thumb.image is not None:

            if list:
                fullsize_url = get_admin_url(obj)
            else:
                if hasattr(obj, 'imageitem'):
                    if obj.imageitem.image is not None:
                        fullsize_url = get_admin_url(obj.imageitem.image)
                    else:
                        fullsize_url = '#'
                else:
                    fullsize_url = '#'
            return pattern.format(fullsize_url, thumb.image.url)
        if list:
            fullsize_url = get_admin_url(obj)
        else:
            fullsize_url = '#'
        return pattern.format(fullsize_url, u'/dolon/media/static/file-by-Gurato.png')
    # end ItemAdmin._format_thumb

    def _format_embed(self, videos):
        if len(videos) == 0:
            return None

        pattern = u'<video width="320" controls>\t{0}</video>'
        spattern = u'<source src="{0}" />'

        # Sort videos so that .MOV format is last.
        videos_ = []
        _mov = None
        for video in videos:
            if hasattr(video.video, 'url'): # May not have downloaded video
                _url = video.video.url       #  content yet.
            else:
                _url = video.url

            fmt = _url.split('.')[-1].lower()   # Not using MIME type, since we
            if fmt == 'mov':                    # may not have that at hand.
                _mov = _url
                continue    # Wait to add this video until the end.
            videos_.append(_url)
        if _mov is not None:    # Add the .MOV file, if there was one.
            videos_.append(_mov)

        vformatted = []
        for _url in videos_:
            vformatted.append(spattern.format(_url))

        return pattern.format(u''.join(vformatted))
    # end ItemAdmin._format_embed

    def _format_audio_embed(self, audios):
        if len(audios) == 0:
            return None
        pattern = u'<audio controls>{0}</audio>'
        spattern = u'<source src="{0}" type="{1}" />'

        aformatted = []
        for audio in audios:
            try:
                aformatted.append(spattern.format(audio.audio_file.url, audio.mime))
            except ValueError:
                aformatted.append(spattern.format(audio.url, ''))
        return pattern.format(u'\n'.join(aformatted))
    # end ItemAdmin._format_audio_embed

    def _item_image(self, obj, list=False):
        """
        Generates a thumbnail image element, with a link to the fullsize
        :class:`.Image`\.
        """

        try:    # If something went wrong when downloading a thumbnail,
                #  this will raise a ValueError.
            if hasattr(obj, 'imageitem'):
                obj.imageitem.thumbnail.image.url
        except:# ValueError, AttributeError:
            return None

        if hasattr(obj, 'imageitem'):
            return self._format_thumb(obj, obj.imageitem.thumbnail, list)
        elif hasattr(obj, 'audioitem'):
            audios = obj.audioitem.audio_segments.all()
            return self._format_audio_embed(audios)
        elif hasattr(obj, 'videoitem'):
            videos = obj.videoitem.videos.all()
            icon = self._format_type_icon('video')

            return self._format_embed(videos)
        elif hasattr(obj, 'textitem'):
            if obj.textitem.snippet is not None:
                if list:
                    return obj.textitem.snippet[0:50]

            return obj.textitem.snippet

    _item_image.allow_tags = True
    # end ItemAdmin._item_image

    def query_events(self, obj):
        """
        Generates a list of :class:`QueryEvent` instances associated with this
        :class:`.Item`\, with links to their respective admin change pages.
        """

        pattern = u'<li><a href="{0}">{1}</a></li>'

        repr = u''.join([ pattern.format(get_admin_url(e), e)
                        for e in obj.events.all() ])
        return u'<ul>{0}</ul>'.format(repr)
    query_events.allow_tags = True
    # end ItemAdmin.query_events
# end ItemAdmin class

class HiddenAdmin(admin.ModelAdmin):
    """
    Not accessible from the admin interface, but individual items are
    accessible.
    """
    def get_model_perms(self, request):
        """
        Return empty perms dict thus hiding the model from admin index.
        """
        return {}
    # end HiddenAdmin.get_model_perms
# end HiddenAdmin class

class ContextAdmin(HiddenAdmin):
    form = autocomplete_light.modelform_factory(Context)
    list_display = ('status', 'diffbot', 'url')
    list_display_links = ('status', 'url')
    readonly_fields = ( 'resource', 'title', 'retrieved', 'diffbot',
                        'use_diffbot', 'publicationDate', 'author', 'language',
                        'text_content',  )
    exclude = ('url','diffbot_requests', 'content')
    actions = (retrieve_context,)

    def diffbot(self, obj):
        try:
            request = obj.diffbot_requests.all()[0]
            if request.completed is not None:
                return '<img src="/dolon/static/admin/img/icon-yes.gif" />'
            return '<img src="/dolon/static/admin/img/icon-no.gif" />'
        except IndexError:
            return '<img src="/dolon/static/admin/img/icon-no.gif" />'
    diffbot.allow_tags = True
    # end ContextAdmin.diffbot

    def queryset(self, request):
        """
        Only return Contexts for approved items in changelist.
        """

        if request.path.split('/')[-2] == 'context':   # Only filter changelist.
            return Context.objects.filter(items__status='AP')
        return super(ContextAdmin, self).queryset(request)
    # end ContextAdmin.queryset

    def resource(self, obj):
        """
        Generates a link to the original context URL, opening in a new tab.
        """
        pattern = u'<a href="{0}" target="_blank">{0}</a>'
        return pattern.format(obj.url)
    resource.allow_tags = True
    # end ContextAdmin.resource

    def status(self, obj):
        """
        Returns True if data for this :class:`.Context` has been retrieved.
        """
        if obj.title is None and obj.content is None:
            return False
        return True
    status.boolean = True
    # end ContextAdmin.status
# end ContextAdmin class

class TagAdmin(admin.ModelAdmin):
#    readonly_fields = ('text', 'items', 'contexts')
    list_display = ('text', 'items', 'contexts')
    search_fields = ['text',]

    def items(self, obj):
        pattern = u'<li><a href="{0}">{1}</a></li>'
        html = u''.join( [ pattern.format(get_admin_url(i),unidecode(i.title)) for i in obj.items() ] )
        return u'<ul>{0}</ul>'.format(html)
    items.allow_tags = True
    # end TagAdmin.items

    def contexts(self, obj):
        pattern = u'<li><a href="{0}">{1}</a></li>'
        for i in obj.contexts():
            print i
        html = u''.join( [ pattern.format(get_admin_url(i),i) for i in obj.contexts() ] )
        return u'<ul>{0}</ul>'.format(html)
    contexts.allow_tags = True
    # end TagAdmin.contexts

    def get_readonly_fields(self, request, obj=None):
        """
        """

        if obj:
            read_only = ('text', 'items', 'contexts') + self.readonly_fields
            return read_only
        return self.readonly_fields
    # end TagAdmin.get_readonly_fields
# end TagAdmin class

class ImageAdmin(HiddenAdmin):
    list_display = ('status', 'url')
    list_display_links = ('status', 'url')
    readonly_fields = ('fullsize_image', 'resource', 'size', 'mime', 'height', 'width')
    exclude = ('url','image')
    actions = (retrieve_image,)

    def queryset(self, request):
        """
        Only return Images for approved items in changelist.
        """

        if request.path.split('/')[-2] == 'image':   # Only filter changelist.
            return Image.objects.filter(queryItems__status='AP')
        return super(ImageAdmin, self).queryset(request)
    # end ImageAdmin.queryset

    def resource(self, obj):
        """
        Generates a link to the original image URL, opening in a new tab.
        """
        pattern = u'<a href="{0}" target="_blank">{0}</a>'
        return pattern.format(obj.url)
    resource.allow_tags = True
    # end ImageAdmin.resource

    def status(self, obj):
        """
        Returns True if data for this :class:`.Image` has been retrieved.
        """
        if obj.size == 0:# and obj.content is None:
            return False
        return True
    status.boolean = True
    # end ImageAdmin.status

    def fullsize_image(self, obj):
        """
        Generates a fullsize image element.

        TODO: constrain display size.
        """

        if obj.image is not None:
            pattern = u'<img src="{0}"/>'
            return pattern.format(obj.image.url)
        return None
    fullsize_image.allow_tags = True
    # end ImageAdmin.fullsize_image
# end ImageAdmin class

class GroupTaskAdmin(admin.ModelAdmin):
    list_display = ('task_id', 'state')
# end GroupTaskAdmin class

class EngineAdmin(admin.ModelAdmin):
    readonly_fields = ['dayusage', 'monthusage']
    list_display = ['engine_name', 'daily_usage', 'monthly_usage']

    def engine_name(self, obj):
        return obj.__unicode__()
    # end EngineAdmin.name

    def daily_usage(self, obj):
        if obj.daylimit is None:
            usage = obj.dayusage
            return u'{0} of unlimited'.format(usage)
        else:
            usage = 100*float(obj.dayusage)/float(obj.daylimit)
            return u'{0}%'.format(usage)
    # end EngineAdmin.daily_usage

    def monthly_usage(self, obj):
        if obj.monthlimit is None:
            usage = obj.monthusage
            return u'{0} of unlimited'.format(usage)
        else:
            usage = 100*float(obj.monthusage)/float(obj.monthlimit)
            return u'{0}%'.format(usage)
    # end EngineAdmin.monthly_usage

    def get_form(self, request, obj=None, **kwargs):
        """
        manager should be readonly when editing.
        """

        readonly_fields = ['dayusage', 'monthusage']
        if obj is not None:
            self.readonly_fields = readonly_fields + ['manager']

        return super(EngineAdmin, self).get_form(request, obj, **kwargs)
    # end EngineAdmin.get_form
# end EngineAdmin class

class AudioAdmin(HiddenAdmin):
    readonly_fields = ['preview', 'url', 'size', 'length', 'mime']
    exclude = ['audio_file']

    def preview(self, obj, *args, **kwargs):
        return self._format_audio_embed(obj)
    preview.allow_tags = True
    # end AudioAdmin.preview

    def _format_audio_embed(self, audio):
        pattern = u'<audio controls>{0}</audio>'
        source = u'<source src="{0}" />'.format(audio.url)

        return pattern.format(source)
    # end AudioAdmin._format_audio_embed
# end AudioAdmin class

class TextAdmin(HiddenAdmin):
    readonly_fields = ['text_file', 'url', 'size', 'mime']
# end TextAdmin class

class ThumbnailAdmin(HiddenAdmin):
    pass
# end ThumbnailAdmin class

class VideoAdmin(HiddenAdmin):
    readonly_fields = ['preview', 'url', 'size', 'length', 'mime']
    exclude = ['video']

    def preview(self, obj, *args, **kwargs):
        return self._format_embed(obj)
    preview.allow_tags = True
    # end VideoAdmin.preview

    def _format_embed(self, video):
        pattern = u'<video width="320" height="240" controls>{0}</video>'
        source = u'<source src="{0}" />'.format(video.url)#, video.type())
        return pattern.format(source)
    # end VideoAdmin._format_embed
# end VideoAdmin class

class DiffBotRequestAdmin(admin.ModelAdmin):
    list_display = ['id', 'created', 'attempted', 'completed', 'type']
    actions = [doPerformDiffBotRequest]
# end DiffBotRequestAdmin class


class OAuthAccessTokenAdmin(admin.ModelAdmin):
    list_display = [    'user_id', 'screen_name', 'platform', 'access_verified',
                        'created', 'expires'   ]
    list_display_links = ['screen_name', 'user_id']

    def access_verified(self, obj):
        """
        Indicate whether OAuth authentication was successful.
        """
        now = localize_datetime(datetime.now())
        
        if obj.oauth_access_token is not None:
            if obj.expires is not None:
                if now >= obj.expires:
                    return '<img src="{0}admin/img/icon-no.gif" />'.format(
                                                        settings.STATIC_URL )
            return '<img src="{0}admin/img/icon-yes.gif" />'.format(
                                                        settings.STATIC_URL )
        return '<img src="{0}admin/img/icon-no.gif" />'.format(
                                                        settings.STATIC_URL )
    access_verified.allow_tags = True

    def get_urls(self):
        """
        Adds the callback view for the OAuth authentication process.
        """
        urls = super(OAuthAccessTokenAdmin, self).get_urls()
        my_urls = patterns('',
            (   r'^callback/(?P<platform>[a-zA-Z]+)/$',
                    self.admin_site.admin_view(self.callback)   ),
        )
        return my_urls + urls
    # end OAuthAccessTokenAdmin.get_urls

    def callback(self, request, platform):
        """
        Receives verifier from OAuth service, and gets an access token.
        """
                               
        if platform == 'Twitter':
            manager = TwitterOAuthManager(
                        consumer_key=settings.TWITTER_KEY,
                        consumer_secret=settings.TWITTER_SECRET
                        )
            _ptoken_id = manager.get_access_token(request)
            ptoken = OAuthAccessToken.objects.get(pk=_ptoken_id)
        elif platform == 'Facebook':
            manager = FacebookOAuthManager(
                        consumer_key=settings.FACEBOOK_ID,
                        consumer_secret=settings.FACEBOOK_SECRET
                        )
            callback_url = 'http://{0}{1}admin/dolon/'.format(request.get_host(), settings.APP_DIR)   +\
                       'oauthaccesstoken/callback/{0}/'.format(platform)                        
            _ptoken_id = manager.get_access_token(request, redirect=callback_url)
            ptoken = OAuthAccessToken.objects.get(pk=_ptoken_id)
        else:
            return
        logger.debug(ptoken)
        return redirect(get_admin_url(ptoken))
            
    # end OAuthAccessTokenAdmin.callback

    def response_add(self, request, obj, post_url_continue=None):
        pattern = 'http://{0}{1}admin/dolon/oauthaccesstoken/callback/{2}/'
        callback_url = pattern.format(
                        request.get_host(), settings.APP_DIR, obj.platform  )

        logger.debug(callback_url)
        
        if obj.platform.name == 'Twitter':
            manager = TwitterOAuthManager(
                        consumer_key=settings.TWITTER_KEY,
                        consumer_secret=settings.TWITTER_SECRET,
                        callback_url = callback_url
                        )
        elif obj.platform.name == 'Facebook':
            manager = FacebookOAuthManager(
                        consumer_key=settings.FACEBOOK_ID,
                        consumer_secret=settings.FACEBOOK_SECRET,
                        callback_url = callback_url
                        )            
        else:
            return
            
        redirect_url = manager.get_access_url(callback_url)            
        return redirect(redirect_url)

    def get_form(self, request, obj=None, **kwargs):
        """
        When creating an OAuthAccessToken,
        """

        exclude = [     'oauth_token_secret', 'oauth_access_token_secret',
                        'oauth_token', 'oauth_verified', 'oauth_access_token',
                        'oauth_verifier'    ]
        if obj is None:
            self.exclude = exclude + [ 'user_id', 'screen_name',
                                       'access_verified', 'creator', 'created' ]
            self.readonly_fields = []
        else:
            self.readonly_fields = [ 'platform',  'user_id', 'access_verified', 
                                     'creator', 'created', ]
            self.exclude = exclude

        form = super(OAuthAccessTokenAdmin, self).get_form( request, obj,
                                                            **kwargs    )

        return form
# end OAuthAccessTokenAdmin class

class SocialUserAdmin(admin.ModelAdmin):
    list_display = ['handle', 'platform', 'user_id', 'profile']


    def get_form(self, request, obj=None, **kwargs):

        if obj is not None:
            self.readonly_fields = [ 'handle', 'platform', 'profile_url',
                                     'user_id', 'content_by_this_user' ]
            self.fields = [ 'handle', 'platform', 'profile_url', 'user_id',
                            'description', 'content_by_this_user' ]                                     
        else:
            self.readonly_fields = []
            self.fields = []
        form = super(SocialUserAdmin, self).get_form(request, obj, **kwargs)
        return form

    def profile(self, obj):
        """
        Generate a link to the user's profile.
        """

        if obj.profile_url is None:
            return None

        link = '<a href="{0}">Profile</a>'.format(obj.profile_url)
        return link
    profile.allow_tags = True

    def content_by_this_user(self, obj):
        """
        Generate a list of :class:`.Item`\s generated by this
        :class:`.SocialUser`\.
        """

        items = obj.content()

        pattern = '<tr class="row{0}">' + \
                        '<td>{1}</td>' + \
                        '<td>{2}</td>' + \
                        '<td><a href="{3}">{4}</a></td>' + \
                    '</tr>'

        lpattern = '<table id="result_list" width="100%"> ' + \
                        '<thead>' + \
                            '<tr>' + \
                                '<td scope="col">Type</td>' + \
                                '<td scope="col">Date</td>' + \
                                '<td scope="col" width="50%">Preview</td>' + \
                            '</tr>' + \
                        '</thead>' + \
                        '<tbody>{0}</tbody>' + \
                    '</table>'

        formatted = []
        row = 2     # Alternating row number.
        for i in items:
            url = get_admin_url(i)
            if i.type == 'Text':
                content = i.textitem.snippet.encode('utf-8')
            else:   # TODO: add support for previewing other content types.
                content = i.title
            formatted.append(
                pattern.format(row, i.type, i.creationDate, url, content)   )

            if row == 1: row = 2
            else: row = 1
        return lpattern.format(''.join(formatted))
    content_by_this_user.allow_tags = True


### Registration ###

admin.site.register(DiffBotRequest, DiffBotRequestAdmin)

admin.site.register(QueryEvent, QueryEventAdmin)
admin.site.register(QueryString, QueryStringAdmin)
admin.site.register(Item, ItemAdmin)
admin.site.register(Engine, EngineAdmin)
admin.site.register(Tag, TagAdmin)

admin.site.register(Context, ContextAdmin)
admin.site.register(Image, ImageAdmin)
admin.site.register(Audio, AudioAdmin)
admin.site.register(Video, VideoAdmin)
admin.site.register(Text, TextAdmin)
admin.site.register(Thumbnail, ThumbnailAdmin)
admin.site.register(SocialPlatform)
admin.site.register(SocialUser, SocialUserAdmin)
admin.site.register(HashTag)
admin.site.register(OAuthAccessToken, OAuthAccessTokenAdmin)