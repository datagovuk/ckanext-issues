import logging
from datetime import datetime

import ckan.logic as logic
import ckan.plugins as p
import ckan.model as model
from ckan.lib import mailer
from ckan.lib.base import render_jinja2
from ckan.logic import validate
import ckan.lib.helpers as h
import ckanext.issues.model as issuemodel
from ckanext.issues.logic import schema
from ckanext.issues.exception import ReportAlreadyExists
from ckanext.issues.lib.helpers import get_issue_subject
try:
    import ckan.authz as authz
except ImportError:
    import ckan.new_authz as authz

from pylons import config
from sqlalchemy.exc import IntegrityError
from sqlalchemy import desc

NotFound = logic.NotFound
_get_or_bust = logic.get_or_bust

log = logging.getLogger(__name__)


def _add_reports(obj, can_edit, current_user):
    reports = [r.user_id for r in obj.abuse_reports]
    if can_edit:
        return reports
    else:
        user_obj = model.User.get(current_user)
        if user_obj and user_obj.id in reports:
            return [user_obj.id]
        else:
            return []


@p.toolkit.side_effect_free
@validate(schema.issue_show_schema)
def issue_show(context, data_dict):
    '''Return a single issue.

    :param dataset_id: the dataset name or id of the issue to show
    :type dataset_id: string
    :param issue_number: the issue number
    :type issue_number: string
    :param include_reports: whether to include abuse reports in the output
    :type include_reports: bool

    :rtype: dictionary
    '''
    session = context['session']
    dataset_id = data_dict['dataset_id']
    issue_number = data_dict['issue_number']
    issue = issuemodel.Issue.get_by_name_or_id_and_number(
        dataset_name_or_id=dataset_id,
        issue_number=issue_number,
        session=session)
    if not issue:
        raise p.toolkit.NotFound(p.toolkit._('Issue does not exist'))

    context['issue'] = issue
    issue_dict = issue.as_dict()

    user = context.get('user')
    if user:
        try:
            can_edit = p.toolkit.check_access(
                'package_update',
                context,
                data_dict={'id': issue.dataset_id}
            )
        except p.toolkit.NotAuthorized:
            can_edit = False
    else:
        can_edit = False

    include_reports = data_dict.get('include_reports')

    comments = []
    for comment in issue.comments:
        comment_dict = comment.as_dict()
        if include_reports:
            comment_dict['abuse_reports'] = _add_reports(comment, can_edit,
                                                         context['user'])
        comments.append(comment_dict)

    issue_dict['comments'] = comments

    p.toolkit.check_access('issue_show', context, issue_dict)
    return issue_dict


def _get_next_issue_number(session, dataset_id):
    q = session.query(issuemodel.Issue)\
        .filter(issuemodel.Issue.dataset_id == dataset_id)\
        .order_by(desc('number')).first()
    if not q:
        return 1
    return q.number + 1


def _get_recipients(context, dataset):
    from ckanext.issues.model.notification import NotificationSettings

    if isinstance(dataset, dict):
        organization = dataset['owner_org']
    else:
        organization = dataset.owner_org

    if organization:
        emails = []

        # Find all users who want every notification and add them
        settings = model.Session.query(NotificationSettings)\
            .filter(NotificationSettings.all_publishers==True).all()
        for setting in settings:
            user_emails = model.Session.query(model.User.id)\
                .filter(model.User.id.in_([s.user_id for s in settings])).all()
            emails.extend([u[0] for u in user_emails])

        settings = model.Session.query(NotificationSettings)\
            .filter(NotificationSettings.include_publishers.like('%"{org}"%'.format(org=organization))).all()
        for setting in settings:
            user_emails = model.Session.query(model.User.id)\
                .filter(model.User.id.in_([s.user_id for s in settings])).all()
            emails.extend([u[0] for u in user_emails])

        roles = authz.get_roles_with_permission('update_dataset')
        recipients = []
        for role in roles:
            recipients += p.toolkit.get_action('member_list')(
                context,
                data_dict={
                    'id': organization,
                    'object_type': 'user',
                    'capacity': role,
                }
            )

        for recipient in recipients:
            if NotificationSettings.does_user_want_notification(recipient[0], organization):
                if not recipient[0] in emails:
                    emails.append(recipient[0])

        return emails

    return []

def _get_issue_vars(issue, issue_subject, user_obj):
    try:
        # from ckan 2.4
        from ckan.model.system_info import get_system_info
        site_title = get_system_info('ckan.site_title')
    except ImportError:
        # older ckans
        site_title = config['ckan.site_title']
    return {'issue': issue,
            'issue_subject': issue_subject,
            'dataset': model.Package.get(issue.dataset_id),
            'user': user_obj,
            'site_title': site_title,
            'h': h}


def _get_notification_settings_token(user_obj):
    from ckanext.issues.model.notification import NotificationToken
    token = NotificationToken.create(user_obj.id)
    return token.code

def _get_issue_email_body(issue, issue_subject, user_obj):
    extra_vars = _get_issue_vars(issue, issue_subject, user_obj)
    # Would use p.toolkit.render, but it mucks with response and other things,
    # which is unnecessary, and p.toolkit.render_text uses genshi...
    extra_vars['settings_token'] = _get_notification_settings_token(user_obj)
    return render_jinja2('issues/email/new_issue.html', extra_vars=extra_vars)


def _get_comment_email_body(comment, issue_subject, user_obj):
    extra_vars = _get_issue_vars(comment.issue, issue_subject, user_obj)
    # The template has to be .html (even though it is .txt) so that
    # it is rendered with jinja
    extra_vars['settings_token'] = _get_notification_settings_token(user_obj)
    return p.toolkit.render('issues/email/new_comment.html',
                            extra_vars=extra_vars)


@validate(schema.issue_create_schema)
def issue_create(context, data_dict):
    '''Add a new issue.

    You must provide your API key in the Authorization header.

    :param title: the title of the issue
    :type title: string
    :param description: the description of the issue item (optional)
    :type description: string
    :param dataset_id: the name or id of the dataset that the issue item
        belongs to (optional)
    :type dataset_id: string

    :returns: the newly created issue item
    :rtype: dictionary
    '''
    p.toolkit.check_access('issue_create', context, data_dict)

    user = context['user']
    user_obj = model.User.get(user)
    data_dict['user_id'] = user_obj.id

    dataset = model.Package.get(data_dict['dataset_id'])
    del data_dict['dataset_id']

    issue = issuemodel.Issue(**data_dict)
    issue.dataset = dataset
    session = context['session']
    issue.number = _get_next_issue_number(session, dataset.id)

    session.add(issue)
    session.commit()

    notifications = p.toolkit.asbool(
        config.get('ckanext.issues.send_email_notifications')
    )

    if notifications:
        recipients = _get_recipients(context, dataset)
        subject = get_issue_subject(issue.as_dict())
        body = _get_issue_email_body(issue, subject, user_obj)

        for recipient in recipients:
            user_obj = model.User.get(recipient)
            try:
                mailer.mail_user(user_obj, subject, body)
            except (mailer.MailerException, TypeError), e:
                # TypeError occurs when we're running command from ckanapi
                log.debug(e.message)

    log.debug('Created issue %s (%s)' % (issue.title, issue.id))
    return issue.as_dict()


@validate(schema.issue_update_schema)
def issue_update(context, data_dict):
    '''Update an issue.

    You must provide your API key in the Authorization header.

    :param title: the title of the issue
    :type title: string
    :param description: the description of the issue item (optional)
    :type description: string
    :param dataset_id: the name or id of the dataset that the issue item
        belongs to (optional)
    :type dataset_id: string
    :param issue_number: the number of the issue.
    :type issue_number: int

    :returns: the newly updated issue item
    :rtype: dictionary
    '''
    p.toolkit.check_access('issue_update', context, data_dict)
    session = context['session']

    issue = issuemodel.Issue.get_by_name_or_id_and_number(
        dataset_name_or_id=data_dict['dataset_id'],
        issue_number=data_dict['issue_number'],
        session=session
    )
    status_change = data_dict.get('status') and (data_dict.get('status') !=
                                                 issue.status)

    # TODO: move to validation?
    ignored_keys = ['id', 'created', 'user', 'dataset_id', 'visibility',
                    'issue_number', '__extras']

    for k, v in data_dict.items():
        if k not in ignored_keys:
            setattr(issue, k, v)

    if status_change:
        if data_dict['status'] == issuemodel.ISSUE_STATUS.closed:
            issue.resolved = datetime.now()
            user = context['user']
            user_dict = p.toolkit.get_action('user_show')(
                data_dict={'id': user})
            issue.assignee_id = user_dict['id']
        elif data_dict['status'] == issuemodel.ISSUE_STATUS.open:
            issue.resolved = None

    session.add(issue)
    session.commit()
    return issue.as_dict()


@validate(schema.issue_delete_schema)
def issue_delete(context, data_dict):
    '''Delete and issues

    :param dataset_id: the name or id of the dataset that the issue item
        belongs to
    :type dataset_id: string
    :param issue_number: the number of the issue.
    :type issue_number: integer
    '''
    p.toolkit.check_access('issue_delete', context, data_dict)
    session = context['session']
    dataset_id = data_dict['dataset_id']
    issue_number = data_dict['issue_number']

    issue = issuemodel.Issue.get_by_name_or_id_and_number(
        dataset_name_or_id=dataset_id,
        issue_number=issue_number,
        session=session
    )
    if not issue:
        raise NotFound(
            '{issue_number} for dataset {dataset_id} was not found.'.format(
                issue_number=issue_number,
                dataset_id=dataset_id,
            )
        )
    session.delete(issue)
    session.commit()


@p.toolkit.side_effect_free
@validate(schema.issue_search_schema)
def issue_search(context, data_dict):
    '''Search issues

    :param dataset_id: the name or id of the dataset that the issue item
        belongs to (optional)
    :type dataset_id: string
    :param organization_id: the name or id of the organization for the datasets
        to filter the issues by (optional)
    :type organization_id: string
    :param include_sub_organizations: if filtering by organization_id, this
        includes organizations below the specified one in the hierarchy.
        (default=False)
    :type include_sub_organizations: bool
    :param q: a query string, currently on searches for titles that match
        this query
    :type q: string
    :param sort: sorting method for the results returned
    :type sort: string, must be 'newest', 'oldest', 'most_commented',
        'least_commented', 'recently_update', 'least_recently_updated'
    :param limit: number of results to return
    :type limit: int
    :param offset: offset of the search results to return
    :type offset: int
    :param visibility: filter on visibility
    :type visibility: string in 'visible', 'hidden', ''
    :param include_datasets: include details of the dataset each issue is
        attached to
    :type include_datasets: bool
    :param include_count: perform an additional query to count the number of
        datasets
    :type include_count: bool
    :param include_results: include dictized results of the issues, you will
        only want to do this if you're just looking to get a count of the
        number of datasets without fetching and dictizing the issue objects
    :type include_results: bool

    :returns: list of issues
    :rtype: list of dictionaries

    '''
    p.toolkit.check_access('issue_search', context, data_dict)
    user = context['user']
    dataset_id = data_dict.get('dataset_id')
    organization_id = data_dict.get('organization_id')
    visibility = 'visible'
    can_update = False
    if organization_id:
        try:
            p.toolkit.check_access('organization_update', context,
                                   data_dict={'id': organization_id})
            visibility = data_dict.get('visibility', None)
            can_update = True
        except p.toolkit.NotAuthorized:
            pass
    elif dataset_id:
        try:
            p.toolkit.check_access('package_update', context,
                                   data_dict={'id': dataset_id})
            visibility = data_dict.get('visibility', None)
            can_update = True
        except p.toolkit.NotAuthorized:
            pass
    elif authz.is_sysadmin(user):
        visibility = data_dict.get('visibility', None)
        can_update = True

    data_dict['visibility'] = visibility
    data_dict.pop('__extras', None)
    include_datasets = p.toolkit.asbool(data_dict.get('include_datasets'))
    include_reports = p.toolkit.asbool(data_dict.get('include_reports'))
    include_count = p.toolkit.asbool(data_dict.pop('include_count', True))
    include_results = p.toolkit.asbool(data_dict.pop('include_results', True))
    data_dict['include_datasets'] = include_datasets

    query = issuemodel.Issue.get_issues(
        session=context['session'],
        **data_dict)

    if include_count:
        count = query.count()
    else:
        count = None

    if include_results:
        results = [issue.as_plain_dict(u, comment_count_, updated,
                                       include_dataset=include_datasets,
                                       include_reports=include_reports)
                   for (issue, u, comment_count_, updated) in query.all()]
    else:
        results = []

    if include_reports and not can_update:
        user_obj = model.User.get(user)
        if user_obj:
            results = _filter_reports_for_user(user_obj.id, results)

    return {
        'count': count,
        'results': results,
    }


def _filter_reports_for_user(user_id, results):
    '''Filter the abuse reports

    If the user does not have update permissions on the dataset, then they will
    only be able to see reports that they have made, this function filters out
    any reports made by other users
    '''
    for result in results:
        if result.get('abuse_reports'):
            if user_id in result['abuse_reports']:
                result['abuse_reports'] = [user_id]
            else:
                result['abuse_reports'] = []
    return results


@validate(schema.issue_comment_schema)
def issue_comment_create(context, data_dict):
    '''Add a new issue comment.

    You must provide your API key in the Authorization header.

    :param comment: the comment text
    :type comment: string
    :param issue_number: the number of the issue the comment belongs to
    :type issue_number: integer
    :param dataset_id: the dataset name or id of the issue the comment belongs to
    :type dataset_id: unicode

    :returns: the newly created issue comment
    :rtype: dictionary
    '''
    p.toolkit.check_access('issue_comment_create', context, data_dict)
    user = context['user']
    user_obj = model.User.get(user)

    issue = issuemodel.Issue.get_by_name_or_id_and_number(
        dataset_name_or_id=data_dict['dataset_id'],
        issue_number=data_dict['issue_number'],
        session=context['session']
    )

    comment_dict = data_dict.copy()
    del comment_dict['dataset_id']
    del comment_dict['issue_number']
    comment_dict.update({
        'user_id': user_obj.id,
        'issue_id': issue.id,
    })

    issue_comment = issuemodel.IssueComment(**comment_dict)
    model.Session.add(issue_comment)
    model.Session.commit()

    notifications = p.toolkit.asbool(
        config.get('ckanext.issues.send_email_notifications')
    )

    if notifications:
        dataset = model.Package.get(data_dict['dataset_id'])
        recipients = _get_recipients(context, dataset)
        subject = get_issue_subject(issue.as_dict())
        body = _get_comment_email_body(issue_comment, subject, user_obj)

        for recipient in recipients:
            user_obj = model.User.get(recipient)
            try:
                mailer.mail_user(user_obj, subject, body)
            except (mailer.MailerException, TypeError), e:
                # TypeError occurs when we're running command from ckanapi
                log.debug(e.message)

    log.debug('Created issue comment %s' % (issue.id))
    return issue_comment.as_dict()


@p.toolkit.side_effect_free
@validate(schema.organization_users_autocomplete_schema)
def organization_users_autocomplete(context, data_dict):
    session = context['session']
    user = context['user']
    q = data_dict['q']
    organization_id = data_dict['organization_id']
    limit = data_dict.get('limit', 20)
    query = session.query(model.User.id, model.User.name,
                          model.User.fullname)\
        .filter(model.Member.group_id == organization_id)\
        .filter(model.Member.table_name == 'user')\
        .filter(model.Member.capacity.in_(['editor', 'admin']))\
        .filter(model.User.state != model.State.DELETED)\
        .filter(model.User.id == model.Member.table_id)\
        .filter(model.User.name.ilike('{0}%'.format(q)))\
        .limit(limit)

    users = []
    for user in query.all():
        user_dict = dict(user.__dict__)
        user_dict.pop('_labels', None)
        users.append(user_dict)
    return users


@validate(schema.issue_report_schema)
def issue_report(context, data_dict):
    '''Report an issue

    If you are a org admin/editor, this marks the comment as abuse; if you are
    any other user, if the number of reports exceeds ckanext.issues.max_strikes
    the issues will be hidden

    :param dataset_id: the name or id of the dataset that the issue item
        belongs to
    :type dataset_id: string
    :param issue_id: the id of the issue the comment belongs to
    :type issue_id: integer
    '''
    p.toolkit.check_access('issue_report', context, data_dict)
    session = context['session']

    issue = issuemodel.Issue.get_by_name_or_id_and_number(
        issue_number=data_dict['issue_number'],
        dataset_name_or_id=data_dict['dataset_id'],
        session=session,
    )
    user_obj = model.User.get(context['user'])
    try:
        issue.report_abuse(session, user_obj.id)
    except IntegrityError:
        session.rollback()
        raise ReportAlreadyExists(
            p.toolkit._('Issue has already been reported by this user')
        )
    try:
        # if you're an org admin/editor (can edit the dataset, it gets marked
        # as abuse immediately
        dataset_id = data_dict['dataset_id']
        context = {
            'user': context['user'],
            'session': session,
            'model': model,
        }
        p.toolkit.check_access('package_update', context,
                               data_dict={'id': dataset_id})

        issue.change_visibility(session, u'hidden')
        issue.abuse_status = issuemodel.AbuseStatus.abuse.value
    except p.toolkit.NotAuthorized:
        max_strikes = config.get('ckanext.issues.max_strikes')
        if (max_strikes
           and len(issue.abuse_reports) >= p.toolkit.asint(max_strikes)):
            issue.change_visibility(session, u'hidden')
    session.commit()


@p.toolkit.side_effect_free
@validate(schema.issue_report_schema)
def issue_report_show(context, data_dict):
    '''Fetch the abuse reports for an issue

    If you are a package owner, this returns the full list of users that have
    reported this issue, otherwise it will return whether the user has marked
    the issue as abuse

    :param dataset_id: the name or id of the dataset that the issue item
        belongs to
    :type dataset_id: string
    :param issue_number: the number of the issue the comment belongs to
    :type issue_number: integer
    '''
    p.toolkit.check_access('issue_report', context, data_dict)
    session = context['session']

    user = context['user']
    user_obj = model.User.get(user)
    user_id = user_obj.id

    dataset_id = data_dict['dataset_id']
    issue_number = data_dict['issue_number']
    issue = issuemodel.Issue.get_by_name_or_id_and_number(
        dataset_name_or_id=dataset_id,
        issue_number=issue_number,
        session=session)

    try:
        package_context = {
            'user': context['user'],
            'session': session,
            'model': model,
        }
        p.toolkit.check_access('package_update', package_context,
                               data_dict={'id': dataset_id})
        reports = issuemodel.Issue.Report.get_reports(session,
                                                      parent_id=issue.id)
    except p.toolkit.NotAuthorized:
        reports = issuemodel.Issue.Report.get_reports_for_user(
            session,
            user_id=user_id,
            parent_id=issue.id
        )

    return [i.user_id for i in reports]


@validate(schema.issue_report_schema)
def issue_report_clear(context, data_dict):
    '''Clears the reports on issue

    :param dataset_id: the name or id of the dataset that the issue item
        belongs to
    :type dataset_id: string
    :param issue_number: the id of the issue the comment belongs to
    :type issue_number: integer
    '''
    p.toolkit.check_access('issue_report_clear', context, data_dict)
    session = context['session']

    issue_number = data_dict['issue_number']
    dataset_id = data_dict['dataset_id']
    issue = issuemodel.Issue.get_by_name_or_id_and_number(
        session=session,
        dataset_name_or_id=dataset_id,
        issue_number=issue_number
    )

    user = context['user']
    user_obj = model.User.get(user)
    user_id = user_obj.id
    try:
        dataset_id = data_dict['dataset_id']
        package_context = {
            'user': context['user'],
            'session': session,
            'model': model,
        }
        p.toolkit.check_access('package_update', package_context,
                               data_dict={'id': dataset_id})
        issue.clear_all_abuse_reports(session)
        issue.abuse_status = issuemodel.AbuseStatus.not_abuse.value
    except p.toolkit.NotAuthorized:
        issue.clear_abuse_report(session, user_id)
        max_strikes = config.get('ckanext.issues.max_strikes')
        if (max_strikes
           and len(issue.abuse_reports) <= p.toolkit.asint(max_strikes)):
            issue.change_visibility(session, u'visible')
    finally:
        session.commit()
    return True


@validate(schema.issue_comment_report_schema)
def issue_comment_report(context, data_dict):
    '''Report a comment made on an issue.

    If you are a org admin/editor, this marks the comment as abuse; if you are
    any other user, if the number of abuse reports exceeds
    ckanext.issues.max_strikes then the issue will be hidden.

    :param comment_id: the id of the issue the comment belongs to
    :type comment_id: integer
    '''
    p.toolkit.check_access('issue_report', context, data_dict)
    session = context['session']

    comment_id = data_dict['comment_id']
    comment = issuemodel.IssueComment.get(comment_id, session=session)
    user_obj = model.User.get(context['user'])
    try:
        comment.report_abuse(session, user_obj.id)
    except IntegrityError:
        session.rollback()
        raise ReportAlreadyExists(
            p.toolkit._('Comment has already been reported by this user')
        )
    try:
        # if you're an org admin/editor (can edit the dataset, it gets marked
        # as abuse immediately
        dataset_id = comment.issue.dataset_id
        package_context = {
            'user': context['user'],
            'session': session,
            'model': model,
        }
        p.toolkit.check_access('package_update', package_context,
                               data_dict={'id': dataset_id})
        comment.change_visibility(session, u'hidden')
        comment.abuse_status = issuemodel.AbuseStatus.abuse.value
    except p.toolkit.NotAuthorized:
        max_strikes = config.get('ckanext.issues.max_strikes')
        if max_strikes:
            if len(comment.abuse_reports) >= p.toolkit.asint(max_strikes):
                comment.change_visibility(session, u'hidden')
    finally:
        session.commit()


@validate(schema.issue_comment_report_schema)
def issue_comment_report_clear(context, data_dict):
    '''Clear the reports on an comment

    :param dataset_id: the name or id of the dataset that the issue item
        belongs to
    :type dataset_id: string
    :param comment_id: the id of the issue the comment belongs to
    :type comment_id: integer
    '''
    p.toolkit.check_access('issue_report_clear', context, data_dict)
    session = context['session']

    comment_id = data_dict['comment_id']
    comment = issuemodel.IssueComment.get(comment_id,
                                          session=session)
    user = context['user']
    user_obj = model.User.get(user)
    user_id = user_obj.id
    try:
        dataset_id = comment.issue.dataset_id
        package_context = {
            'user': context['user'],
            'session': session,
            'model': model,
        }
        p.toolkit.check_access('package_update', package_context,
                               data_dict={'id': dataset_id})
        comment.clear_all_abuse_reports(session)
        comment.abuse_status = issuemodel.AbuseStatus.not_abuse.value
    except p.toolkit.NotAuthorized:
        comment.clear_abuse_report(session, user_id)
        max_strikes = config.get('ckanext.issues.max_strikes')
        if (max_strikes and
           len(comment.abuse_reports) <= p.toolkit.asint(max_strikes)):
            comment.change_visibility(session, u'visible')
    finally:
        session.commit()
    return True


@p.toolkit.side_effect_free
def issue_comment_search(context, data_dict):
    p.toolkit.check_access('issue_comment_search', context, data_dict)
    session = context['session']

    organization_id = data_dict['organization_id']

    reported_comments = issuemodel.IssueComment.reported_comments(
        session,
        organization_id=organization_id
    )

    comments = []
    for comment, issue in reported_comments.all():
        comment_dict = comment.as_dict()
        comment_dict.update({
            'dataset_id': issue.dataset_id,
            'issue_number': issue.number,
        })
        comments.append(comment_dict)

    return comments

