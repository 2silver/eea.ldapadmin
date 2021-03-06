# encoding: utf-8
import unittest
import re
import sys
import logging
from copy import deepcopy
import csv
from StringIO import StringIO
from mock import Mock, patch
import lxml.cssselect, lxml.html.soupparser
from eea.ldapadmin.roles_editor import RolesEditor, CommonTemplateLogic
from eea.ldapadmin.ui_common import TemplateRenderer
from eea import usersdb
from eea.ldapadmin import roles_editor

def plaintext(element):
    return re.sub(r'\s\s+', ' ', element.text_content()).strip()

def css(target, selector):
    return lxml.cssselect.CSSSelector(selector)(target)

def csstext(target, selector):
    return ' '.join(e.text_content() for e in css(target, selector)).strip()

def parse_html(html):
    return lxml.html.soupparser.fromstring(html)

def stubbed_renderer():
    renderer = TemplateRenderer(CommonTemplateLogic)
    renderer.wrap = lambda html: "<html>%s</html>" % html
    return renderer


class StubbedRolesEditor(RolesEditor):
    def __init__(self):
        super(StubbedRolesEditor, self).__init__()
        self._render_template = stubbed_renderer()

    def absolute_url(self):
        return "URL"

def mock_request():
    request = Mock()
    request.SESSION = {}
    return request

user_map_fixture = {
    'jsmith': {
        'id': "jsmith",
        'full_name': "Joe Smith",
        'email': u"jsmith@example.com",
        'phone': u"555 1234",
        'fax': u"555 6789",
        'organisation': "My company",
    },
    'anne': {
        'id': "anne",
        'full_name': "Anne Tester",
        'email': u"anne@example.com",
        'phone': u"555 32879",
        'fax': u"",
        'organisation': "Some Agency",
    },
}

org_map_fixture = {
    'bridge_club': {
        'id': 'bridge_club',
        'name': u"Ye olde bridge club",
        'url': u"http://bridge-club.example.com/",
    },
    'poker_club': {
        'id': 'poker_club',
        'name': u"P\xf8ker club",
        'url': u"http://poker-club.example.com/",
    },
}

user_info_fixture = user_map_fixture['jsmith']

from test_orgs_ui import org_info_fixture

def session_messages(request):
    return request.SESSION.get('eea.ldapadmin.roles_editor.messages')

class BrowseTest(unittest.TestCase):
    def setUp(self):
        self.ui = StubbedRolesEditor()
        self.mock_agent = Mock()
        self.ui._get_ldap_agent = Mock(return_value=self.mock_agent)
        self.request = mock_request()
        self.request.form = {'role_id': 'places'}
        self.ui.REQUEST = self.request
        user = self.request.AUTHENTICATED_USER
        user.getRoles.return_value = ['Authenticated']
        self.mock_agent.members_in_role.return_value = {'users':[], 'orgs':[]}
        self.mock_agent.role_names_in_role.return_value = {}
        self.mock_agent.mail_group_info.return_value = {
            'owner': [],
            'permittedSender': ['anyone'],
            'permittedPerson': [],
        }
        self.mock_agent.role_info.return_value = {
            'description': "Various places",
        }

    def test_browse_subroles(self):
        role_names = {'bank': "Bank for Test", 'agency': "The Agency"}
        self.mock_agent.role_names_in_role.return_value = role_names

        page = parse_html(self.ui.index_html(self.request))

        self.mock_agent.role_names_in_role.assert_called_with('bank')
        table = page.xpath('table/tbody')[0]
        self.assertEqual(len(table.xpath('.//tr')), 2)
        tr1, tr2 = table.xpath('tr')
        self.assertEqual(plaintext(tr1.xpath('td[1]')[0]), "agency")
        self.assertEqual(plaintext(tr1.xpath('td[2]')[0]), "The Agency")
        self.assertEqual(plaintext(tr2.xpath('td[1]')[0]), "bank")
        self.assertEqual(plaintext(tr2.xpath('td[2]')[0]), "Bank for Test")

    def test_browse_role_info(self):
        page = parse_html(self.ui.index_html(self.request))

        self.assertEqual(page.xpath('//h1')[0].text, "Various places")
        self.mock_agent.role_info.assert_called_with('places')

    def test_user_info(self):
        self.mock_agent.members_in_role.return_value = {
            'users': ['jsmith'], 'orgs': [],
        }
        self.mock_agent.user_info.return_value = dict(user_info_fixture)

        page = parse_html(self.ui.index_html(self.request))

        self.mock_agent.members_in_role.assert_called_once_with('places')
        self.mock_agent.user_info.assert_called_once_with('jsmith')

        cells = page.xpath('table[@class="account-datatable dataTable"]/tbody/tr/td')
        self.assertEqual(plaintext(cells[0]), user_info_fixture['full_name'])
        self.assertEqual(plaintext(cells[1]), 'jsmith')
        self.assertEqual(plaintext(cells[2]), user_info_fixture['email'])
        self.assertEqual(plaintext(cells[4]), user_info_fixture['organisation'])

    def test_missing_role(self):
        exc = usersdb.RoleNotFound("no-such-role")
        self.mock_agent.role_info.side_effect = exc
        self.request.form = {'role_id': 'no-such-role'}

        page = parse_html(self.ui.index_html(self.request))

        self.assertEqual(plaintext(page.xpath('//p[@class="message"]')[0]),
                         "Role no-such-role does not exist.")
        self.request.RESPONSE.setStatus.assert_called_once_with(404)


class CreateDeleteRolesTest(unittest.TestCase):
    def setUp(self):
        self.ui = StubbedRolesEditor()
        self.mock_agent = Mock()
        self.mock_agent._encoding = 'utf-8'
        self.ui._get_ldap_agent = Mock(return_value=self.mock_agent)
        self.request = mock_request()
        user = self.request.AUTHENTICATED_USER
        user.getRoles.return_value = ['Authenticated']
        self.ui.REQUEST = self.request
        def agent_role_id(role_dn):
            assert role_dn.startswith('test-dn:')
            return role_dn[len('test-dn:'):]
        self.mock_agent._role_id = agent_role_id
        self.mock_agent.mail_group_info.return_value = {
            'owner': [],
            'permittedSender': ['anyone'],
            'permittedPerson': [],
        }

        self.stream = StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.log = logging.getLogger('roles_editor')
        self.log.setLevel(logging.INFO)
        for handler in self.log.handlers:
            self.log.removeHandler(handler)
        self.log.addHandler(self.handler)

    def tearDown(self):
        self.log = logging.getLogger('roles_editor')
        self.log.removeHandler(self.handler)
        self.handler.close()

    def test_link_from_browse(self):
        self.mock_agent.members_in_role.return_value = {'users':[], 'orgs':[]}
        self.mock_agent.role_names_in_role.return_value = {}
        self.mock_agent.role_info.return_value = {
            'description': "Various places",
        }
        self.request.form = {'role_id': 'places'}

        page = parse_html(self.ui.index_html(self.request))

        create_url = "URL/create_role_html?parent_role_id=places"
        create_links = page.xpath('//a[@href="%s"]' % create_url)
        self.assertEqual(len(create_links), 1)
        self.assertEqual(plaintext(create_links[0]), "Create sub-role")

        delete_url = "URL/delete_role_html?role_id=places"
        delete_links = page.xpath('//a[@href="%s"]' % delete_url)
        self.assertEqual(len(delete_links), 1)
        self.assertEqual(plaintext(delete_links[0]), "Delete role places")

    def test_create_role_html(self):
        self.request.form = {'parent_role_id': 'places'}

        page = parse_html(self.ui.create_role_html(self.request))

        self.assertEqual(plaintext(page.xpath('//h1')[0]),
                         "Create role under places")
        self.assertEqual(page.xpath('//form')[0].attrib['action'],
                         "URL/create_role")
        input_parent = page.xpath('//form//input[@name="parent_role_id"]')[0]
        self.assertEqual(input_parent.attrib['value'], 'places')
        input_desc_xpath = '//form//input[@name="description:utf8:ustring"]'
        self.assertEqual(len(page.xpath(input_desc_xpath)), 1)

    @patch('eea.ldapadmin.roles_editor.logged_in_user')
    def test_create_role_submit(self, logged_user):
        logged_user.return_value = "john_doe"
        self.request.form = {'parent_role_id': 'places', 'slug': 'shiny',
                             'description': "Shiny new role"}

        self.ui.create_role(self.request)
    
        self.mock_agent.create_role.assert_called_once_with(
            'places-shiny', "Shiny new role")
        self.mock_agent.add_role_owner.assert_called_once_with(
            'places-shiny', "john_doe")
        self.request.RESPONSE.redirect.assert_called_with(
            'URL/?role_id=places-shiny')

        msg = 'Created role places-shiny "Shiny new role"'
        logmsg = ("john_doe CREATED ROLE places-shiny\n"
                  "john_doe ADDED john_doe OWNER for ROLE places-shiny\n")
        self.assertEqual(session_messages(self.request), {'info': [msg]})
        self.assertEqual(self.stream.getvalue(), logmsg)

    @patch('eea.ldapadmin.roles_editor.logged_in_user')
    def test_create_role_submit_unicode(self, logged_user):
        logged_user.return_value = "john_doe"
        self.request.form = {'parent_role_id': 'places', 'slug': 'shiny',
                             'description': u"Shiny new r\u014dle"}
        self.ui.create_role(self.request)
        self.mock_agent.create_role.assert_called_once_with(
            'places-shiny', u"Shiny new r\u014dle")
        self.mock_agent.add_role_owner.assert_called_once_with(
            'places-shiny', "john_doe")

    def test_create_role_submit_invalid(self):
        self.request.form = {'parent_role_id': 'places', 'slug': 'SHINY',
                             'description': "Shiny new role"}
        self.ui.create_role(self.request)
        self.assertEqual(self.mock_agent.create_role.call_count, 0)

        self.request.RESPONSE.redirect.assert_called_with(
            'URL/create_role_html?parent_role_id=places')

        msg = ("Invalid Role ID, it must contain only lowercase "
               "latin letters.")
        self.assertEqual(session_messages(self.request), {'error': [msg]})

        self.request.form = {'parent_role_id': 'places'}
        #print self.ui.create_role_html(self.request)
        page = parse_html(self.ui.create_role_html(self.request))

        role_id_xp = '//form//input[@name="slug:utf8:ustring"]'
        role_desc_xp = '//form//input[@name="description:utf8:ustring"]'
        self.assertEqual(page.xpath(role_id_xp)[0].attrib['value'], 'SHINY')
        self.assertEqual(page.xpath(role_desc_xp)[0].attrib['value'],
                         "Shiny new role")

    def test_delete_role_html(self):
        self.request.form = {'role_id': 'places-bank'}
        self.mock_agent._sub_roles.return_value = [
            'test-dn:places-bank',
            'test-dn:places-bank-central',
            'test-dn:places-bank-branch',
        ]

        page = parse_html(self.ui.delete_role_html(self.request))

        self.mock_agent._sub_roles.assert_called_once_with('places-bank')
        self.assertEqual([plaintext(li) for li in
                          page.xpath('//form//tbody/tr')],
                         ['places-bank',
                          'places-bank-central',
                          'places-bank-branch'])

    @patch('eea.ldapadmin.roles_editor.logged_in_user')
    def test_delete_role(self, logged_user):
        logged_user.return_value = "John Doe"
        self.request.form = {'role_id': 'places-bank'}
        self.mock_agent.members_in_role_and_subroles.return_value = {'users': []}

        self.ui.delete_role(self.request)

        self.mock_agent.delete_role.assert_called_once_with('places-bank')
        self.mock_agent.members_in_role_and_subroles.assert_called_once_with('places-bank')
        self.request.RESPONSE.redirect.assert_called_with(
            'URL/?role_id=places')
        logmsg = "John Doe DELETED ROLE places-bank\n"
        self.assertEqual(self.stream.getvalue(), logmsg)

class AddRemoveRoleMembersTest(unittest.TestCase):
    def setUp(self):
        self.ui = StubbedRolesEditor()
        self.mock_agent = Mock()
        self.mock_agent._encoding = 'utf-8'
        self.ui._get_ldap_agent = Mock(return_value=self.mock_agent)
        self.mock_agent.mail_group_info.return_value = {
            'owner': [],
            'permittedSender': ['anyone'],
            'permittedPerson': [],
        }
        self.request = mock_request()
        user = self.request.AUTHENTICATED_USER
        user.getRoles.return_value = ['Authenticated']
        self.ui.REQUEST = self.request

        self.stream = StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.log = logging.getLogger('roles_editor')
        self.log.setLevel(logging.INFO)
        for handler in self.log.handlers:
            self.log.removeHandler(handler)
        self.log.addHandler(self.handler)

    def tearDown(self):
        self.log = logging.getLogger('roles_editor')
        self.log.removeHandler(self.handler)
        self.handler.close()

    def test_links(self):
        self.mock_agent.members_in_role.return_value = {'users':[], 'orgs':[]}
        self.mock_agent.role_names_in_role.return_value = {}
        self.mock_agent.role_info.return_value = {
            'description': "Various places",
        }
        self.request.form = {'role_id': 'places'}

        page = parse_html(self.ui.index_html(self.request))

        add_member_url = "URL/add_member_html?role_id=places"
        add_member_links = page.xpath('//a[@href="%s"]' % add_member_url)
        self.assertEqual(len(add_member_links), 1)
        self.assertEqual(plaintext(add_member_links[0]), "Add members")

        rm_members_url = "URL/remove_members_html?role_id=places"
        rm_members_links = page.xpath('//a[@href="%s"]' % rm_members_url)
        self.assertEqual(len(rm_members_links), 1)
        self.assertEqual(plaintext(rm_members_links[0]), "Remove members")

    def test_add_user_html(self):
        self.request.form = {'role_id': 'places'}

        page = parse_html(self.ui.add_member_html(self.request))

        self.assertEqual(self.mock_agent.search_user.call_count, 0)
        self.assertEqual(plaintext(page.xpath('//h1')[0]),
                         "Add members to role places")

    def test_add_user_search_html(self):
        self.request.form = {'role_id': 'places', 'name': 'smith'}
        self.mock_agent.search_user.return_value = [user_info_fixture]
        self.mock_agent.search_org.return_value = []

        page = parse_html(self.ui.add_member_html(self.request))

        self.mock_agent.search_user.assert_called_once_with('smith')
        name = plaintext(page.xpath('//tbody/tr/td')[0])
        self.assertEqual(name, "Joe Smith jsmith@example.com")
        form = page.xpath('//form[@name="add-user"]')[0]
        self.assertEqual(form.attrib['action'], 'URL/add_user')

    def test_add_user_search_html_no_results(self):
        self.request.form = {'role_id': 'places', 'name': 'smith'}
        self.mock_agent.search_user.return_value = []
        self.mock_agent.search_org.return_value = []

        page = parse_html(self.ui.add_member_html(self.request))

        self.assertEqual(plaintext(page.cssselect('p.search-message')[0]),
                         "No matching users for smith.")

    @patch('eea.ldapadmin.roles_editor.logged_in_user')
    def test_add_user_submit(self, logged_user):
        logged_user.return_value = 'John Doe'
        self.request.form = {'role_id': 'places-bank', 'user_id': 'jsmith'}
        self.mock_agent.add_to_role.return_value = ['places', 'places-bank']
        self.mock_agent.role_info.return_value = {
            'description': "Various places",
        }

        self.ui.add_user(self.request)
        self.mock_agent.add_to_role.assert_called_once_with(
            'places-bank', 'user', 'jsmith')
        self.request.RESPONSE.redirect.assert_called_with(
            'URL/?role_id=places-bank')
        msg = "User 'jsmith' added to roles Various places, Various places."
        logmsg = "John Doe ADDED USER jsmith to ROLE(S) ['places', 'places-bank']\n"
        self.assertEqual(session_messages(self.request), {'info': [msg]})
        self.assertEqual(self.stream.getvalue(), logmsg)

    def test_remove_users_html(self):
        self.mock_agent.members_in_role.return_value = {'users':['jsmith'],
                                                        'orgs':[]}
        self.mock_agent.role_names_in_role.return_value = {}
        self.mock_agent.role_info.return_value = {
            'description': "Various places",
        }
        self.mock_agent.user_info.return_value = dict(user_info_fixture,
                                                      id='jsmith')
        self.request.form = {'role_id': 'places'}

        page = parse_html(self.ui.remove_members_html(self.request))

        remove_form = page.xpath('//form[@name="remove-users"]')[0]
        self.assertEqual(remove_form.attrib['action'], 'URL/remove_members')
        user_tr = remove_form.xpath('.//tbody/tr')[0]
        user_checkbox = user_tr.xpath('.//input[@name="user_id_list:list"]')[0]
        self.assertEqual(user_checkbox.attrib['value'], 'jsmith')

    @patch('eea.ldapadmin.roles_editor.logged_in_user')
    def test_remove_users_submit(self, logged_user):
        logged_user.return_value = 'John Doe'
        self.request.form = {'role_id': 'places', 'user_id_list': ['jsmith']}
        self.mock_agent.role_info.return_value = {
            'description': "Various places",
        }
        self.mock_agent.remove_from_role.return_value = ['places']

        self.ui.remove_members(self.request)

        self.request.RESPONSE.redirect.assert_called_with(
            'URL/?role_id=places')
        msg = "Users ['jsmith'] removed from role 'Various places'"
        logmsg = "John Doe REMOVED USER jsmith FROM ROLES ['places']\n"
        self.assertEqual(session_messages(self.request), {'info': [msg]})
        self.assertEqual(self.stream.getvalue(), logmsg)

    @patch('eea.ldapadmin.roles_editor.logged_in_user')
    def test_remove_users_submit_nothing(self, logged_user):
        logged_user.return_value = 'John Doe'
        self.request.form = {'role_id': 'places'}
        self.mock_agent.role_info.return_value = {
            'description': "Various places",
        }

        self.ui.remove_members(self.request)

        self.request.RESPONSE.redirect.assert_called_with(
            'URL/?role_id=places')
        self.assertEqual(session_messages(self.request), None)

class UserSearchTest(unittest.TestCase):
    def setUp(self):
        self.ui = StubbedRolesEditor()
        self.mock_agent = Mock()
        self.mock_agent._encoding = 'utf-8'
        self.mock_agent.is_subrole = lambda r1, r2: r1.startswith(r2)
        self.ui._get_ldap_agent = Mock(return_value=self.mock_agent)
        self.request = mock_request()
        user = self.request.AUTHENTICATED_USER
        user.getRoles.return_value = ['Authenticated']
        self.ui.REQUEST = self.request

        self.stream = StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.log = logging.getLogger('roles_editor')
        self.log.setLevel(logging.INFO)
        for handler in self.log.handlers:
            self.log.removeHandler(handler)
        self.log.addHandler(self.handler)

    def tearDown(self):
        self.log = logging.getLogger('roles_editor')
        self.log.removeHandler(self.handler)
        self.handler.close()

    def test_plain(self):
        self.request.form = {}
        page = parse_html(self.ui.search_users(self.request))

        form = page.xpath('//form[@name="search-users"]')[0]
        self.assertEqual(len(form.xpath('.//input[@name="name:ustring:utf8"]')), 1)

    def test_results(self):
        self.request.form = {'name': 'smith'}
        self.mock_agent.search_user.return_value = [user_info_fixture]

        page = parse_html(self.ui.search_users(self.request))

        self.mock_agent.search_user.assert_called_once_with('smith')
        name = plaintext(page.xpath('//ul/li/span[@class="user-name"]')[0])
        self.assertEqual(name, "Joe Smith")

    def test_no_result(self):
        self.request.form = {'name': 'smith'}
        self.mock_agent.search_user.return_value = []

        page = parse_html(self.ui.search_users(self.request))

        self.assertEqual(plaintext(page.xpath('//p[@class="no-results"]')[0]),
                         "Found no users matching smith.")

    def test_user_roles_html(self):
        self.request.form = {'user_id': 'jsmith'}
        self.mock_agent.list_member_roles.return_value = ['places-bank',
                                                          'places-bank-branch']

        page = parse_html(self.ui.search_users(self.request))

        roles_div = page.xpath('//div[@class="user-roles"]')[0]
        self.assertEqual(plaintext(roles_div.xpath('h2')[0]),
                         "User jsmith is a member of:")
        self.mock_agent.list_member_roles.assert_called_once_with(
            'user', 'jsmith')
        self.assertEqual([plaintext(li) for li in roles_div.xpath('ul/li')],
                         ['places-bank', 'places-bank-branch'])

    def test_user_remove_from_role_html(self):
        self.request.form = {'role_id': 'places', 'user_id': 'jsmith'}
        self.mock_agent.list_member_roles.return_value = [
            'places', 'places-bank', 'places-bank-central']

        page = parse_html(self.ui.remove_user_from_role_html(self.request))

        self.assertEqual(plaintext(page.xpath('//h1')[0]), "Revoke membership")
        self.assertEqual(page.xpath('//form')[0].attrib['action'],
                         'URL/remove_user_from_role')
        self.assertEqual(plaintext(page.xpath('//form//p')[0]),
                         "Remove jsmith from the following roles:")
        self.assertEqual([plaintext(li) for li in page.xpath('//form/ul/li')],
                         ['places', 'places-bank', 'places-bank-central'])

    @patch('eea.ldapadmin.roles_editor.logged_in_user')
    def test_user_remove_from_role_submit(self, logged_user):
        logged_user.return_value = "John Doe"
        self.request.form = {'role_id': 'places', 'user_id': 'jsmith'}
        role_ids = ['places-bank', 'places-bank-central']
        self.mock_agent.remove_from_role.return_value = role_ids

        self.ui.remove_user_from_role(self.request)

        self.mock_agent.remove_from_role.assert_called_once_with(
            'places', 'user', 'jsmith')
        self.request.RESPONSE.redirect.assert_called_with(
             'URL/search_users?user_id=jsmith')
        msg = ("User 'jsmith' removed from roles "
               "'places-bank', 'places-bank-central'.")
        logmsg = "John Doe REMOVED USER 'jsmith' FROM ROLE(S) ['places-bank', 'places-bank-central']\n"
        self.assertEqual(session_messages(self.request), {'info': [msg]})
        self.assertEqual(self.stream.getvalue(), logmsg)

class FilterTest(unittest.TestCase):
    def setUp(self):
        self.ui = StubbedRolesEditor()
        self.mock_agent = Mock()
        self.mock_agent._encoding = 'utf-8'
        self.ui._get_ldap_agent = Mock(return_value=self.mock_agent)
        self.request = mock_request()
        user = self.request.AUTHENTICATED_USER
        user.getRoles.return_value = ['Authenticated']
        self.ui.REQUEST = self.request

        role_membership = {
            'places-bank': {'users': sorted(user_map_fixture.keys()),
                            'orgs': sorted(org_map_fixture.keys())},
            'places-shiny': {'users': [], 'orgs': []},
        }
        self.mock_agent.filter_roles.return_value = [(x, {}) for x in role_membership.keys()]
        self.mock_agent.members_in_role.side_effect = role_membership.get
        self.mock_agent.user_info.side_effect = deepcopy(user_map_fixture).get
        self.mock_agent.org_info.side_effect = deepcopy(org_map_fixture).get

    def check_query_results(self, page):
        self.assertEqual([plaintext(tt) for tt in page.xpath('//h3')],
                         ["Users in places-bank", 'Users in places-shiny'])
        # TODO we should also list places-shiny somehow

        user_names = [plaintext(s) for s in
                      page.xpath('//table[1]/tbody/tr/td[1]')]
        expected_user_names = [user_map_fixture[user_id]['full_name']
                               for user_id in sorted(user_map_fixture)]
        self.assertEqual(user_names, expected_user_names)

    def test_filter_html(self):
        self.request.form = {'pattern': 'places-*'}

        page = parse_html(self.ui.filter(self.request))

        pattern_input = page.xpath('//form/input[@type="search"]')[0]
        self.assertEqual(pattern_input.attrib['value'], 'places-*')
        self.check_query_results(page)

    def test_no_results(self):
        self.request.form = {'pattern': 'places-*'}
        self.mock_agent.filter_roles.return_value = []

        page = parse_html(self.ui.filter(self.request))

        self.assertEqual(plaintext(page.cssselect('p.search-message')[0]),
                         "No roles found matching places-*.")

class UserInfoTest(unittest.TestCase):

    def setUp(self):
        self.ui = StubbedRolesEditor()
        self.mock_agent = Mock()
        self.mock_agent._encoding = 'utf-8'
        self.ui._get_ldap_agent = Mock(return_value=self.mock_agent)
        self.request = mock_request()
        self.user = self.request.AUTHENTICATED_USER
        self.user.getRoles.return_value = ['Anonymous']
        self.ui.REQUEST = self.request
        self.users = deepcopy(user_map_fixture)
        self.mock_agent.user_info.side_effect = self.users.get
        self.mock_agent.role_info.return_value = {
            'description': "The bank",
        }
        self.mock_agent.mail_group_info.return_value = {
            'owner': [],
            'permittedSender': ['anyone'],
            'permittedPerson': [],
        }
        self.mock_agent.role_names_in_role.return_value = {}
        self.role_membership = {'places-bank': {
            'users': ['jsmith'], 'orgs': []}}
        self.mock_agent.filter_roles.side_effect = lambda *args, **kw: \
            [(x, {}) for x in self.role_membership.keys()]
        self.mock_agent.members_in_role.side_effect = self.role_membership.get
        #self.mock_agent.user_info.side_effect = deepcopy(user_map_fixture).get
        #self.mock_agent.org_info.side_effect = deepcopy(org_map_fixture).get

    def _get_fields(self, name):
        page = parse_html(getattr(self.ui, name)(self.request))
        txt = lambda e: e.text_content().strip()
        table = css(page, 'table.account-datatable')[0]
        labels = map(txt, css(table, 'thead tr td'))
        values = map(txt, css(table, 'tbody tr td'))
        self.assertEqual(len(labels), len(values))
        data = dict(zip(labels, values))
        return data

        self.assertEqual(data)


    def test_not_logged_in(self):
        # not-logged-in users can see just the name

        # "view role" page
        self.request.form = {'role_id': 'places-bank'}
        self.assertEqual(self._get_fields('index_html'), {'Name': "Joe Smith"})

        # "search" page
        self.request.form = {'pattern': 'places-*'}
        self.assertEqual(self._get_fields('filter'), {'Name': "Joe Smith"})

    def test_logged_in(self):
        # logged-in users can see more info

        def assert_full_info(data):
            self.assertEqual(data['Name'],  "Joe Smith")
            self.assertEqual(data['User ID'], "jsmith")
            self.assertEqual(data['Email'], "jsmith@example.com")
            self.assertEqual(data['Tel/Fax'], "555 1234\n\n555 6789")
            self.assertEqual(data['Organisation'], "My company")

        self.user.getRoles.return_value = ['Authenticated']

        # "view role" page
        self.request.form = {'role_id': 'places-bank'}
        assert_full_info(self._get_fields('index_html'))

        # "search" page
        self.request.form = {'pattern': 'places-*'}
        assert_full_info(self._get_fields('filter'))

    def test_filter_users_csv(self):
        self.users['confucius'] = {'id': 'confucius',
            'full_name': u"孔子", 'organisation': u"儒家",
            'email': "", 'phone': "", 'fax': ""}
        self.role_membership['places-china'] = {
            'users': ['confucius'], 'orgs': []}
        self.request.form = {'pattern': 'places-*'}
        expected_rows = [
            ['Role','Name', 'User ID', 'Email', 'Tel/Fax', 'Organisation'],
            ["places-bank", "Joe Smith", "jsmith", "jsmith@example.com",
             "555 1234, 555 6789", "My company"],
            ["places-china", u"孔子".encode('utf-8'), "confucius",
             "", "", u"儒家".encode('utf-8')],
        ]

        # not logged in
        self.assertEqual(self.ui.filter_users_csv(self.request),
                         "You must be logged in to access this page.\n")

        # logged in
        self.user.getRoles.return_value = ['Authenticated']
        data = StringIO(self.ui.filter_users_csv(self.request))
        rows = list(csv.reader(data))
        self.assertEqual(rows[0], expected_rows[0])
        self.assertEqual(sorted(rows[1:]), sorted(expected_rows[1:]))
