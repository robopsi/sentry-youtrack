# -*- encoding: utf-8 -*-
from hashlib import md5
import json

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Fieldset
from django import forms
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.utils.translation import ugettext_lazy as _
from sentry.models import GroupMeta
from sentry.plugins.bases.issue import IssuePlugin
from sentry.utils.cache import cache
from requests.exceptions import HTTPError, ConnectionError

from sentry_youtrack.youtrack import YouTrackClient
from sentry_youtrack import VERSION


class YouTrackProjectForm(forms.Form):

    PROJECT_FIELD_PREFIX = 'field_'

    FIELD_TYPE_MAPPING = {
        'float': forms.FloatField,
        'integer': forms.IntegerField,
        'date': forms.DateField,
        'string': forms.CharField,
    }

    project_field_names = {}

    def __init__(self, project_fields=None, *args, **kwargs):
        super(YouTrackProjectForm, self).__init__(*args, **kwargs)
        if not project_fields is None:
            self.add_project_fields(project_fields)

    def add_project_fields(self, project_fields):
        fields = []
        for field in project_fields:
            form_field = self._get_form_field(field)
            if form_field:
                index = len(fields) + 1
                field_name = '%s%s' % (self.PROJECT_FIELD_PREFIX, index)
                self.fields[field_name] = form_field
                fields.append(form_field)
                self.project_field_names[field_name] = field['name']
        return fields

    def get_project_field_values(self):
        self.full_clean()
        values = {}
        for form_field_name, name in self.project_field_names.iteritems():
            values[name] = self.cleaned_data.get(form_field_name)
        return values

    def _get_initial(self, field_name):
        default_fields = self.initial.get('default_fields', {})
        field_key = md5(field_name).hexdigest()
        if field_key in default_fields.keys():
            return default_fields.get(field_key)

    def _get_form_field(self, project_field):
        field_type = project_field['type']
        field_values = project_field['values']
        form_field = self.FIELD_TYPE_MAPPING.get(field_type)

        kwargs = {
            'label': project_field['name'],
            'required': False,
            'initial': self._get_initial(project_field['name'])
        }
        if form_field:
            return form_field(**kwargs)
        if field_values:
            choices = zip(field_values, field_values)
            if "[*]" in field_type:
                return forms.MultipleChoiceField(
                    widget=forms.CheckboxSelectMultiple,
                    choices=choices, **kwargs)
            kwargs['choices'] = [('', '-----')] + choices
            return forms.ChoiceField(**kwargs)


class YouTrackNewIssueForm(YouTrackProjectForm):

    title = forms.CharField(
        label=_("Title"),
        widget=forms.TextInput(attrs={'class': 'span9'})
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"class": 'span9'})
    )
    tags = forms.CharField(
        label=_("Tags"),
        help_text=_("Comma-separated list of tags"),
        widget=forms.TextInput(attrs={'class': 'span6', 'placeholder': "e.g. sentry"}),
        required=False
    )

    def clean_description(self):
        description = self.cleaned_data.get('description')

        description = description.replace('```', '{quote}')

        return description


class YouTrackAssignIssueForm(forms.Form):

    issue = forms.CharField(
        label=_("YouTrack Issue"),
        widget=forms.TextInput(attrs={'class': 'span6',
                                      'placeholder': _("Choose issue")}))


class DefaultFieldForm(forms.Form):
    field = forms.CharField(required=True, max_length=255)
    value = forms.CharField(required=True, max_length=255)


class YoutrackConfigurationForm(forms.Form):

    url = forms.URLField(
        label=_("YouTrack Instance URL"),
        widget=forms.TextInput(attrs={'class': 'span9', 'placeholder': 'e.g. "https://youtrack.myjetbrains.com/"'}),
        required=True
    )
    username = forms.CharField(
        label=_("Username"),
        help_text=_("User should have admin rights."),
        widget=forms.TextInput(attrs={'class': 'span9'}),
        required=True
    )
    password = forms.CharField(
        label=_("Password"),
        help_text=_("Only enter a password if you want to change it"),
        widget=forms.PasswordInput(attrs={'class': 'span9'}),
        required=False
    )
    project = forms.ChoiceField(
        label=_("Linked Project"),
        required=True
    )
    default_tags = forms.CharField(
        label=_("Default tags"),
        help_text=_("Comma-separated list of tags"),
        widget=forms.TextInput(attrs={'class': 'span6', 'placeholder': "e.g. sentry"}),
        required=False
    )
    ignore_fields = forms.MultipleChoiceField(
        label=_("Ignore fields"),
        required=False,
        help_text=_("These fields will not appear on the form")
    )

    def __init__(self, *args, **kwargs):
        super(YoutrackConfigurationForm, self).__init__(*args, **kwargs)

        client = None
        initial = kwargs.get("initial")

        if initial:
            client = self.get_youtrack_client(initial)
            if not client and not args[0]:
                self.full_clean()
                self._errors['username'] = [self.youtrack_client_error]

        fieldsets = [
            Fieldset(
                None,
                'url',
                'username',
                'password',
                'project',
            )
        ]

        if initial and client:
            fieldsets.append(
                Fieldset(
                    _("Create issue"),
                    'default_tags',
                    'ignore_fields')
            )

            if initial.get('project'):
                fields = client.get_project_fields_list(initial.get('project'))
                names = [field['name'] for field in fields]
                choices = zip(names, names)
                self.fields['ignore_fields'].choices = choices

            projects = [(' ', u"- Choose project -")]
            for project in client.get_projects():
                projects.append((project['shortname'], u"%s (%s)" % (project['name'], project['shortname'])))
            self.fields["project"].choices = projects

            if not any(args) and not initial.get('project'):
                self.second_step_msg = u"%s %s" % (_("Your credentials are valid but plugin is NOT active yet."),
                                                   _("Please fill in remaining required fields."))

        else:
            del self.fields["project"]
            del self.fields["default_tags"]
            del self.fields["ignore_fields"]

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(*fieldsets)

    def get_youtrack_client(self, data):
        yt_settings = {
            'url': data.get('url'),
            'username': data.get('username'),
            'password': data.get('password'),
        }

        client = None

        try:
            client = YouTrackClient(**yt_settings)
        except (HTTPError, ConnectionError) as e:
            self.youtrack_client_error = u"%s %s" % (_("Unable to connect to YouTrack."), e)
        else:
            try:
                client.get_user(yt_settings.get('username'))
            except HTTPError as e:
                if e.response.status_code == 403:
                    self.youtrack_client_error = _("User doesn't have Low-level Administration permissions.")
                    client = None

        return client

    def clean_password(self):
        password = self.cleaned_data.get('password') or self.initial.get('password')

        if not password:
            raise ValidationError(_("This field is required."))

        return password

    def clean_project(self):
        project = self.cleaned_data.get('project').strip()

        if not project:
            raise ValidationError(_("This field is required."))

        return project

    def clean(self):
        data = self.cleaned_data

        if not all(data.get(field) for field in ('url', 'username', 'password')):
            raise ValidationError(_('Missing required fields'))

        client = self.get_youtrack_client(data)
        if not client:
            self._errors['username'] = self.error_class([self.youtrack_client_error])
            del data['username']

        return data


def cache_this(timeout=60):
    def decorator(func):
        def wrapper(*args, **kwargs):
            def get_cache_key(*args, **kwargs):
                params = list(args) + kwargs.values()
                return md5("".join(map(str, params))).hexdigest()
            key = get_cache_key(func.__name__, *args, **kwargs)
            result = cache.get(key)
            if not result:
                result = func(*args, **kwargs)
                cache.set(key, result, timeout)
            return result
        return wrapper
    return decorator


class YouTrackPlugin(IssuePlugin):
    author = u"Adam Bogdał"
    author_url = "https://github.com/bogdal/sentry-youtrack"
    version = VERSION
    slug = "youtrack"
    title = _("YouTrack")
    conf_title = title
    conf_key = slug
    new_issue_form = YouTrackNewIssueForm
    assign_issue_form = YouTrackAssignIssueForm
    create_issue_template = "sentry_youtrack/create_issue_form.html"
    assign_issue_template = "sentry_youtrack/assign_issue_form.html"
    project_conf_form = YoutrackConfigurationForm
    project_conf_template = "sentry_youtrack/project_conf_form.html"
    project_fields_form = YouTrackProjectForm
    default_fields_key = 'default_fields'

    resource_links = [
        (_("Bug Tracker"), "https://github.com/bogdal/sentry-youtrack/issues"),
        (_("Source"), "http://github.com/bogdal/sentry-youtrack"),
    ]
    
    def is_configured(self, request, project, **kwargs):
        return bool(self.get_option('project', project))

    def get_youtrack_client(self, project):
        settings = {
            'url': self.get_option('url', project),
            'username': self.get_option('username', project),
            'password': self.get_option('password', project),
        }
        return YouTrackClient(**settings)

    def get_project_fields(self, project):

        @cache_this(600)
        def cached_fields(ignore_fields):
            yt_client = self.get_youtrack_client(project)
            return yt_client.get_project_fields(
                self.get_option('project', project), ignore_fields)

        return cached_fields(self.get_option('ignore_fields', project))

    def get_initial_form_data(self, request, group, event, **kwargs):
        initial = {
            'title': self._get_group_title(request, group, event),
            'description': self._get_group_description(request, group, event),
            'tags': self.get_option('default_tags', group.project),
            'default_fields': self.get_option(self.default_fields_key,
                                              group.project)
        }
        return initial

    def get_new_issue_title(self):
        return _("Create YouTrack Issue")

    def get_existing_issue_title(self):
        return _("Assign existing YouTrack issue")

    def get_new_issue_form(self, request, group, event, **kwargs):
        if request.POST or request.GET.get('form'):
            project_fields = self.get_project_fields(group.project)
            return self.new_issue_form(project_fields,
                                       data=request.POST or None,
                                       initial=self.get_initial_form_data(
                                           request, group, event))
        return forms.Form()

    def create_issue(self, request, group, form_data, **kwargs):

        project_fields = self.get_project_fields(group.project)
        project_form = self.project_fields_form(project_fields, request.POST)
        project_field_values = project_form.get_project_field_values()

        tags = filter(None, map(lambda x: x.strip(),
                                form_data['tags'].split(',')))

        yt_client = self.get_youtrack_client(group.project)
        issue_data = {
            'project': self.get_option('project', group.project),
            'summary': form_data.get('title'),
            'description': form_data.get('description'),
        }

        issue_id = yt_client.create_issue(issue_data)['id']

        for field, value in project_field_values.iteritems():
            if value:
                value = [value] if type(value) != list else value
                cmd = map(lambda x: "%s %s" % (field, x), value)
                yt_client.execute_command(issue_id, " ".join(cmd))

        if tags:
            yt_client.add_tags(issue_id, tags)

        return issue_id

    def get_issue_url(self, group, issue_id, **kwargs):
        url = self.get_option('url', group.project)
        return "%sissue/%s" % (url, issue_id)

    def get_view_response(self, request, group):
        if request.is_ajax() and request.GET.get('action'):
            return self.view(request, group)
        return super(YouTrackPlugin, self).get_view_response(request, group)

    def actions(self, request, group, action_list, **kwargs):
        action_list = (super(YouTrackPlugin, self)
                       .actions(request, group, action_list, **kwargs))

        prefix = self.get_conf_key()
        if not GroupMeta.objects.get_value(group, '%s:tid' % prefix, None):
            url = self.get_url(group) + "?action=assign_issue"
            action_list.append((self.get_existing_issue_title(), url))
        return action_list

    def view(self, request, group, **kwargs):
        def get_action_view():
            action_view = "%s_view" % request.GET.get('action')
            if request.GET.get('action') and hasattr(self, action_view):
                return getattr(self, action_view)

        view = get_action_view() or super(YouTrackPlugin, self).view
        return view(request, group, **kwargs)

    def assign_issue_view(self, request, group):
        form = self.assign_issue_form(request.POST or None)

        if form.is_valid():
            issue_id = form.cleaned_data['issue']
            prefix = self.get_conf_key()
            GroupMeta.objects.set_value(group, '%s:tid' % prefix, issue_id)

            return self.redirect(reverse('sentry-group',
                                         args=[group.team.slug,
                                               group.project_id,
                                               group.pk]))

        context = {
            'form': form,
            'title': self.get_existing_issue_title(),
        }
        return self.render(self.assign_issue_template, context)

    def project_issues_view(self, request, group):
        project_issues = []
        query = request.POST.get('q')

        def get_int(value, default=0):
            try:
                return int(value)
            except ValueError:
                return default

        page = get_int(request.POST.get('page'), 1)
        page_limit = get_int(request.POST.get('page_limit'), 15)
        offset = (page-1) * page_limit

        yt_client = self.get_youtrack_client(group.project)
        project_id = self.get_option('project', group.project)
        issues = yt_client.get_project_issues(project_id,
                                              offset=offset,
                                              limit=page_limit + 1,
                                              query=query or None)

        for issue in issues:
            project_issues.append({
                'id': issue['id'],
                'state': issue.find("field", {'name': 'State'}).text,
                'summary': issue.find("field", {'name': 'summary'}).text})

        data = {
            'more': len(issues) > page_limit,
            'issues': project_issues[:page_limit]
        }
        return HttpResponse(json.dumps(data, cls=DjangoJSONEncoder))

    def save_field_as_default_view(self, request, group):
        form = DefaultFieldForm(request.POST or None)
        if form.is_valid():
            field = form.cleaned_data.get('field')
            value = form.cleaned_data.get('value')
            default_fields = self.get_option(self.default_fields_key,
                                             group.project) or {}
            default_fields[md5(field).hexdigest()] = value
            self.set_option(self.default_fields_key,
                            default_fields,
                            group.project)
        return HttpResponse()
