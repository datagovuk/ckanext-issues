"""
For a given user, this table model defines whether the user wishes to
receive notifications for a given publisher. If there is no database
entry for the user then it is expected the caller will check against
configuration.

The settings are exclusive.

1. Always send me NotificationSettings
2. Only send me notifications where I am an admin or editor
3. Only send me notifications for the following publishers.

"""
from datetime import datetime
import json
import uuid

from sqlalchemy import types, Table, Column
from sqlalchemy.exc import IntegrityError

import ckan.model  as model
from ckan.model import domain_object, meta

def make_uuid():
    return unicode(uuid.uuid4())

class NotificationToken(domain_object.DomainObject):

    @classmethod
    def create(self, user_id, created=None):
        # Cleanup existing tokens for this user
        existing = model.Session.query(NotificationToken)\
            .filter(NotificationToken.user_id == user_id)\
            .delete()

        token = NotificationToken(user_id=user_id,
                                                  code=make_uuid(),
                                                  created=created)
        model.Session.add(token)
        model.Session.commit()
        return token

    @classmethod
    def validate_token(self, code):
        token = model.Session.query(NotificationToken)\
            .filter(NotificationToken.code == code).first()

        if not token:
            return False, None

        age = datetime.now() - token.created
        if age.days > 30:
            return False, None

        return True, token.user_id


class NotificationSettings(domain_object.DomainObject):

    @classmethod
    def create(self, **kwargs):
        n = NotificationSettings()
        for k, v in kwargs.iteritems():
            if isinstance(v, list):
                setattr(n, k, json.dumps(v))
            else:
                setattr(n, k, v)

        if not n.user_id:
            raise ValueError("user_id is required")

        model.Session.add(n)
        model.Session.commit()

        return n

    @classmethod
    def find_record(self, user_id):
        return model.Session.query(NotificationSettings)\
            .filter(NotificationSettings.user_id==user_id)\
            .first()

    @classmethod
    def add_publisher(self, user_id, publisher_id):
        """
        Add a publisher_id to the list of publisher_ids that this user wants
        to receive notifications from (or not).
        """
        record = self.find_record(user_id)
        if not record:
            record = NotificationSettings.create(user_id=user_id)

        publishers = json.loads(record.include_publishers)
        if publisher_id not in publishers:
            publishers.append(publisher_id)
            record.include_publishers = json.dumps(publishers)
            model.Session.add(record)
            model.Session.commit()

    @classmethod
    def does_user_want_notification(self,  user_id, publisher_id):
        """
        Checks if any of the user's settings specify that they are not
        interested in receiving notifications for this publisher_id. Returns
        two booleans, the first specifying whether a record with found
        and consulted, the second whether the user wants a notification
        or not.
        """
        record = self.find_record(user_id)
        if not record:
            # Setup default record for this user ...
            record = NotificationSettings.create(user_id=user_id,
                                                                     all_where_editor_admin=True)

        explicit_yes = json.loads(record.include_publishers)

        if record.all_publishers:
            return True

        if publisher_id in explicit_yes:
            return True

        if record.all_where_editor_admin:
            # See if the user_id is a member of the publisher_id specified ...
            member_count = model.Session.query(model.Member)\
                .filter(model.Member.table_id == user_id)\
                .filter(model.Member.group_id == publisher_id)\
                .filter(model.Member.state == 'active').count()
            return member_count == 1

        return False

def define_notification_token_table():
    table = Table(
        'issue_notification_tokens',
        meta.metadata,
        Column('id', types.Unicode, default=make_uuid, primary_key=True,
            autoincrement=True),
        Column('user_id', types.Unicode, nullable=False, unique=False),
        Column('code', types.Unicode, nullable=False, unique=True),
        Column('created', types.DateTime, default=datetime.now,
            nullable=False)
    )

    meta.mapper( NotificationToken, table, properties={})
    return table


def define_notification_table():
    table = Table(
        'issue_notifications',
        meta.metadata,
        Column('id', types.Unicode, default=make_uuid, primary_key=True,
            autoincrement=True),
        Column('user_id', types.Unicode, nullable=False, unique=False),

        # Does the user want notifications from all publishers where they are
        # an admin or an editor
        Column('all_where_editor_admin', types.Boolean, default=False),

        # Does the user just want all notifications for all publishers
        Column('all_publishers', types.Boolean, default=False),

        # Contains a json list of publisher ids which
        # are the publishers that the user explicitly chose to receive notifications
        # from.  _true means always, _false means never.
        Column('include_publishers', types.Unicode, default=u"[]"),

        Column('created', types.DateTime, default=datetime.now,
            nullable=False)
    )

    meta.mapper( NotificationSettings, table, properties={})
    return table
