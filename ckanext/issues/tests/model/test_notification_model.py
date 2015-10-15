import json
from datetime import datetime

from ckan import model
from ckan.plugins import toolkit
import ckan.new_tests.helpers as helpers
import ckan.new_tests.factories as factories

from ckanext.issues.tests import factories as issue_factories
from ckanext.issues.tests.helpers import (
    ClearOnTearDownMixin,
    ClearOnSetupClassMixin
)
from ckanext.issues.model.notification import (NotificationSettings,
                                                                          NotificationToken)

from nose.tools import assert_true, raises


class TestNotificationTokenModel(ClearOnTearDownMixin, ClearOnSetupClassMixin):

    def setup(self):
        self.user = factories.User()

    def test_create(self):
        token = NotificationToken.create(self.user['id'])
        assert token.user_id == self.user['id']
        assert token.code
        assert token.created

    def test_create_with_existing(self):
        token = NotificationToken.create(self.user['id'])
        code = token.code
        when = token.created

        token = NotificationToken.create(self.user['id'])
        assert token.code != code
        assert token.created != when

    def test_invalid_code(self):
        token = NotificationToken.create(self.user['id'])
        assert NotificationToken.validate_token("invalid") == (False, None,)

    def test_valid_code(self):
        token = NotificationToken.create(self.user['id'])
        code = token.code
        assert NotificationToken.validate_token(code) == (True, self.user["id"],)

    def test_old_token(self):
        old_date = datetime(year=2014, month=12, day=31)
        token = NotificationToken.create(self.user['id'], created=old_date)
        code = token.code

        success, user = NotificationToken.validate_token(code)
        assert success == False
        assert user is None


class TestNotificationModel(ClearOnTearDownMixin, ClearOnSetupClassMixin):

    def setup(self):
        self.user = factories.User()
        self.organization = factories.Organization()

    def test_create_notification(self):
        s = NotificationSettings.create(user_id=self.user['id'])
        assert s is not None, s
        assert s.user_id == self.user['id'], s

    @raises(ValueError)
    def test_create_notification_missing_value(self):
        _ = NotificationSettings.create()

    def test_add_publisher_creates_record(self):
        assert NotificationSettings.find_record(self.user['id']) == None
        NotificationSettings.add_publisher(self.user['id'], self.organization['id'])
        assert NotificationSettings.find_record(self.user['id']) is not None

    def test_user_no_record(self):
        assert NotificationSettings.find_record('rhubarb') == None

    def test_user_requested_all(self):
        _ = NotificationSettings.create(user_id=self.user['id'], all_publishers=True)
        s = NotificationSettings.find_record(self.user['id'])
        assert s.all_publishers == True

        found, wants = s.does_user_want_notification(self.user['id'], self.organization['id'])
        assert found == True
        assert wants == True

    def test_includes(self):
        s = NotificationSettings.create(user_id=self.user['id'],
                                                         include_publishers=[self.organization['id']]
                                                         )
        assert s.all_publishers == False
        assert len(json.loads(s.include_publishers)) == 1

        found, wants = s.does_user_want_notification(self.user['id'], self.organization['id'])
        assert found == True
        assert wants == True

    def test_where_admin(self):
        local_organization = factories.Organization(
            users=[{'name': self.user['id'], 'capacity': 'editor'}]
        )

        s = NotificationSettings.create(user_id=self.user['id'],
                                                        all_where_editor_admin=True)
        found, wants = s.does_user_want_notification(self.user['id'],
                                                                                  local_organization['id'])
        #assert found == True
        assert wants == True

    def test_no_record(self):
        assert NotificationSettings.find_record(self.user['id']) == None
