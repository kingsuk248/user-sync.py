import logging
from collections import defaultdict

import six

from user_sync import error, identity_type
from user_sync.config.common import DictConfig
from user_sync.connector.connector_sign import SignConnector
from user_sync.engine.umapi import AdobeGroup
from user_sync.error import AssertionException
from user_sync.helper import normalize_string


class SignSyncEngine:
    default_options = {
        'admin_roles': None,
        'create_users': False,
        'directory_group_filter': None,
        'entitlement_groups': [],
        'identity_types': [],
        'new_account_type': identity_type.FEDERATED_IDENTITY_TYPE,
        'sign_only_limit': 200,
        'sign_orgs': [],
        'test_mode': False,
        'user_groups': []
    }
    name = 'sign_sync'
    DEFAULT_GROUP_NAME = 'default group'

    def __init__(self, caller_options):
        super().__init__()
        options = dict(self.default_options)
        options.update(caller_options)
        self.options = options
        self.logger = logging.getLogger(self.name)
        self.test_mode = options.get('test_mode')
        sync_config = DictConfig('<%s configuration>' % self.name, caller_options)
        self.user_groups = options['user_groups'] = sync_config.get_list('user_groups', True)
        if self.user_groups is None:
            self.user_groups = []
        self.user_groups = self._groupify(self.user_groups)
        self.entitlement_groups = self._groupify(sync_config.get_list('entitlement_groups'))
        self.identity_types = sync_config.get_list('identity_types', True)
        if self.identity_types is None:
            self.identity_types = ['adobeID', 'enterpriseID', 'federatedID']
        self.directory_user_by_user_key = {}
        # dict w/ structure - umapi_name -> adobe_group -> [set of roles]
        self.admin_roles = self._admin_role_mapping(sync_config)

        # builder = user_sync.config.common.OptionsBuilder(sync_config)
        # builder.set_string_value('logger_name', self.name)
        # builder.set_bool_value('test_mode', False)
        # options = builder.get_options()

        sign_orgs = sync_config.get_list('sign_orgs')
        self.connectors = {cfg.get('console_org'): SignConnector(cfg) for cfg in sign_orgs}
        self.create_new_users = sync_config.get_bool("create_new_users")
        self.deactivate_sign_only_users = sync_config.get_bool("deactivate_sign_only_users")

    def run(self, directory_groups, directory_connector):
        """
        Run the Sign sync
        :param directory_groups:
        :param directory_connector:
        :return:
        """
        if self.test_mode:
            self.logger.info("Sign Sync disabled in test mode")
            return
        directory_users = self.read_desired_user_groups(directory_groups, directory_connector)
        if directory_users is None:
            raise AssertionException("Error retrieving users from directory")
        for org_name, sign_connector in self.connectors.items():
            # create any new Sign groups
            for new_group in set(self.user_groups[org_name]) - set(sign_connector.sign_groups()):
                self.logger.info("Creating new Sign group: {}".format(new_group))
                sign_connector.create_group(new_group)
            self.update_sign_users(directory_users, sign_connector, org_name)
            if self.deactivate_sign_only_users:
                self.deactivate_sign_users(directory_users, sign_connector)

    def update_sign_users(self, directory_users, sign_connector, org_name):
        sign_users = sign_connector.get_users()
        for _, directory_user in directory_users.items():
            sign_user = sign_users.get(directory_user['email'])
            if not self.should_sync(directory_user, org_name):
                continue

            assignment_group = None

            for group in self.user_groups[org_name]:
                if group in directory_user['groups']:
                    assignment_group = group
                    break

            if assignment_group is None:
                assignment_group = self.DEFAULT_GROUP_NAME

            group_id = sign_connector.get_group(assignment_group)
            admin_roles = self.admin_roles.get(org_name, {})
            user_roles = self.resolve_new_roles(directory_user, admin_roles)
            if self.create_new_users is True and sign_user is None:
                self.insert_new_users(sign_connector, directory_user, user_roles, group_id, assignment_group)
            if sign_user is None: # sign_user may still be None here is flag 'create_new_users' is False and user does not exist
                continue
            else:
                self.update_existing_users(sign_connector, sign_user, directory_user, group_id, user_roles, assignment_group)

    @staticmethod
    def roles_match(resolved_roles, sign_roles):
        if isinstance(sign_roles, str):
            sign_roles = [sign_roles]
        return sorted(resolved_roles) == sorted(sign_roles)

    @staticmethod
    def resolve_new_roles(umapi_user, role_mapping):
        roles = set()
        for group in umapi_user['groups']:
            sign_roles = role_mapping.get(group.lower())
            if sign_roles is None:
                continue
            roles.update(sign_roles)
        return list(roles) if roles else ['NORMAL_USER']

    def should_sync(self, umapi_user, org_name):
        """
        Initial gatekeeping to determine if user is candidate for Sign sync
        Any checks that don't depend on the Sign record go here
        Sign record must be defined for user, and user must belong to at least one entitlement group
        and user must be accepted identity type
        :param umapi_user:
        :param org_name:
        :return:
        """
        return set(umapi_user['groups']) & set(self.entitlement_groups[org_name]) and \
               umapi_user['type'] in self.identity_types

    @staticmethod
    def _groupify(groups):
        processed_groups = defaultdict(list)
        for g in groups:
            processed_group = AdobeGroup.create(g)
            processed_groups[processed_group.umapi_name].append(processed_group.group_name.lower())
        return processed_groups

    @staticmethod
    def _admin_role_mapping(sync_config):
        admin_roles = sync_config.get_list('admin_roles', True)
        if admin_roles is None:
            return {}

        mapped_admin_roles = {}
        for mapping in admin_roles:
            sign_role = mapping.get('sign_role')
            if sign_role is None:
                raise AssertionException("must define a Sign role in admin role mapping")
            adobe_groups = mapping.get('adobe_groups')
            if adobe_groups is None or not len(adobe_groups):
                continue
            for g in adobe_groups:
                group = AdobeGroup.create(g)
                group_name = group.group_name.lower()
                if group.umapi_name not in mapped_admin_roles:
                    mapped_admin_roles[group.umapi_name] = {}
                if group_name not in mapped_admin_roles[group.umapi_name]:
                    mapped_admin_roles[group.umapi_name][group_name] = set()
                mapped_admin_roles[group.umapi_name][group_name].add(sign_role)
        return mapped_admin_roles

    def read_desired_user_groups(self, mappings, directory_connector):
        # this is only the first part of the same method in engine/umapi
        # going to make it return the modified directory users list
        self.logger.debug('Building work list...')

        options = self.options
        directory_group_filter = options['directory_group_filter']
        if directory_group_filter is not None:
            directory_group_filter = set(directory_group_filter)
        extended_attributes = options.get('extended_attributes')

        directory_user_by_user_key = self.directory_user_by_user_key

        directory_groups = set(six.iterkeys(mappings))
        if directory_group_filter is not None:
            directory_groups.update(directory_group_filter)
        directory_users = directory_connector.load_users_and_groups(groups=directory_groups,
                                                                    extended_attributes=extended_attributes,
                                                                    all_users=directory_group_filter is None)

        for directory_user in directory_users:
            user_key = self.get_directory_user_key(directory_user)
            if not user_key:
                self.logger.warning("Ignoring directory user with empty user key: %s", directory_user)
                continue
            directory_user_by_user_key[user_key] = directory_user

            # if not self.is_directory_user_in_groups(directory_user, directory_group_filter):
            #     continue
            # if not self.is_selected_user_key(user_key):
            #     continue

        return directory_user_by_user_key

    def get_directory_user_key(self, directory_user):
        """
        Identity-type aware user key management for directory users
        :type directory_user: dict
        """
        id_type = self.get_identity_type_from_directory_user(directory_user)
        return self.get_user_key(id_type, directory_user['username'], directory_user['domain'], directory_user['email'])

    def get_user_key(self, id_type, username, domain, email=None):
        """
        Construct the user key for a directory or adobe user.
        The user key is the stringification of the tuple (id_type, username, domain)
        but the domain part is left empty if the username is an email address.
        If the parameters are invalid, None is returned.
        :param username: (required) username of the user, can be his email
        :param domain: (optional) domain of the user
        :param email: (optional) email of the user
        :param id_type: (required) id_type of the user
        :return: string "id_type,username,domain" (or None)
        :rtype: str
        """
        id_type = identity_type.parse_identity_type(id_type)
        email = normalize_string(email) if email else None
        username = normalize_string(username) or email
        domain = normalize_string(domain)

        if not id_type:
            return None
        if not username:
            return None
        if username.find('@') >= 0:
            domain = ""
        elif not domain:
            return None
        return six.text_type(id_type) + u',' + six.text_type(username) + u',' + six.text_type(domain)

    def get_identity_type_from_directory_user(self, directory_user):
        identity_type = directory_user.get('identity_type')
        if identity_type is None:
            identity_type = self.options['new_account_type']
            self.logger.warning('Found user with no identity type, using %s: %s', identity_type, directory_user)
        return identity_type

    def update_existing_users(self, sign_connector, sign_user, directory_user, group_id, user_roles, assignment_group):
            update_data = {
                "email": sign_user['email'],
                "firstName": sign_user['firstName'],
                "groupId": group_id,
                "lastName": sign_user['lastName'],
                "roles": user_roles,
            }
            if sign_user['group'].lower() == assignment_group and self.roles_match(user_roles, sign_user['roles']):
                self.logger.debug("skipping Sign update for '{}' -- no updates needed".format(directory_user['email']))
                return
            try:
                sign_connector.update_user(sign_user['userId'], update_data)
                self.logger.info("Updated Sign user '{}', Group: '{}', Roles: {}".format(
                    directory_user['email'], assignment_group, update_data['roles']))
            except AssertionError as e:
                self.logger.error("Error updating user {}".format(e))

    def insert_new_users(self, sign_connector, directory_user, user_roles, group_id, assignment_group):
        """
        Inserts new user in the Sign Console
        :param sign_connector:
        :param directory_user:
        :param user_roles:
        :param group_id:
        :param assignment_group:
        :return:
        """
        insert_data = {
            "email": directory_user['email'],
            "firstName": directory_user['firstname'],
            "groupId": group_id,
            "lastName": directory_user['lastname'],
            "roles": user_roles,
        }
        try:
            sign_connector.insert_user(insert_data)
            self.logger.info("Inserted Sign user '{}', Group: '{}', Roles: {}".format(
            directory_user['email'], assignment_group, insert_data['roles']))
        except AssertionException as e:
            self.logger.error(format(e))
        return

    def deactivate_sign_users(self, directory_users, sign_connector):
        sign_users = sign_connector.get_users()
        director_users_emails = []
        for directory_user in directory_users.values():
            director_users_emails.append(directory_user['email'].lower())
        for _, sign_user in sign_users.items():
            if sign_user['email'].lower() not in director_users_emails:
                self.deactivate_user(sign_connector, sign_user)

    def deactivate_user(self, sign_connector, sign_user):
        deactivation_data = {
                "userStatus": 'INACTIVE'
        }
        try:
            sign_connector.deactivate_user(sign_user['userId'], deactivation_data)
        except AssertionException as e:
            self.logger.error("Error deactivating user {}, {}".format(sign_user['email'], e))
        return

