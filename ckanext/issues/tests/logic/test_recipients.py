from ckan import model
from ckan.plugins import toolkit
import ckan.new_tests.helpers as helpers
import ckan.new_tests.factories as factories

from ckanext.issues.logic.action.action import _get_recipients as get_recipients
from ckanext.issues.model.notification import NotificationSettings
from ckanext.issues.tests import factories as issue_factories
from ckanext.issues.tests.helpers import (
    ClearOnTearDownMixin,
    ClearOnSetupClassMixin
)

from nose.tools import assert_true, assert_raises


class TestRecipients(ClearOnTearDownMixin, ClearOnSetupClassMixin):

    @classmethod
    def setup(self):
        self.editor_wo_record = factories.User(email='fake@localhost.local')
        self.editor_all = factories.User(email='fake@localhost.local')
        NotificationSettings.create(user_id=self.editor_all['id'], all_publishers=True)

        self.no_members_org = factories.Organization()
        self.no_members_dataset = factories.Dataset(owner_org=self.no_members_org['id'])

        self.org = factories.Organization(
            name="test",
            users=[
                {'name': self.editor_wo_record['id'], 'capacity': 'editor'},
                {'name': self.editor_all['id'], 'capacity': 'admin'},
            ]
        )
        self.dataset = factories.Dataset(owner_org=self.org['id'])
        self.context = {"model": model, "session": model.Session}

        self.interested_user = factories.User(email='fake@localhost.local')
        NotificationSettings.create(user_id=self.interested_user['id'], include_publishers=[self.org['id']])


    def test_should_send_no_record(self):
        o = model.Session.query(model.Group).filter(model.Group.id==self.org['id']).first()
        user_ids = get_recipients(self.context, self.dataset)
        assert len(user_ids) == 3, user_ids
        assert self.editor_wo_record['id'] in user_ids

    def test_editor_wants_all(self):
        user_ids = get_recipients(self.context, self.no_members_dataset)
        assert len(user_ids) == 1, len(user_ids)   # Interested user and editor
        assert self.editor_all['id'] in user_ids

    def test_user_wants_only_org(self):
        user_ids = get_recipients(self.context, self.dataset)
        assert len(user_ids) == 3, len(user_ids)
        assert self.interested_user['id'] in user_ids

    def test_user_wants_specific(self):
        user_ids = get_recipients(self.context, self.no_members_dataset)
        assert self.interested_user['id'] not in user_ids
