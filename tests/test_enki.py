from nose.tools import (
    assert_raises,
    assert_raises_regexp,
    set_trace,
    eq_,
    assert_not_equal,
    raises,
)
import datetime
import os
import pkgutil
import json
from core.model import (
    CirculationEvent,
    Contributor,
    DataSource,
    ExternalIntegration,
    LicensePool,
    Resource,
    Hyperlink,
    Identifier,
    Edition,
    Timestamp,
    Subject,
    Measurement,
    Work,
)
from . import DatabaseTest
from api.authenticator import BasicAuthenticationProvider
from api.circulation import LoanInfo
from api.circulation_exceptions import *
from api.config import CannotLoadConfiguration
from api.enki import (
    EnkiAPI,
    MockEnkiAPI,
    EnkiImport,
    BibliographicParser,
)
from core.metadata_layer import (
    CirculationData,
    Metadata,
)
from core.scripts import RunCollectionCoverageProviderScript
from core.util.http import (
    BadResponseException,
    RequestTimedOut,
)
from core.testing import MockRequestsResponse

class BaseEnkiTest(DatabaseTest):

    base_path = os.path.split(__file__)[0]
    resource_path = os.path.join(base_path, "files", "enki")

    @classmethod
    def get_data(cls, filename):
        path = os.path.join(cls.resource_path, filename)
        return open(path).read()

    def setup(self):
        super(BaseEnkiTest, self).setup()
        self.api = MockEnkiAPI(self._db)
        self.collection = self.api.collection


class TestEnkiAPI(BaseEnkiTest):

    def test_constructor(self):

        bad_collection = self._collection(
            protocol=ExternalIntegration.OVERDRIVE
        )
        assert_raises_regexp(
            ValueError,
            "Collection protocol is Overdrive, but passed into EnkiAPI!",
            EnkiAPI, self._db, bad_collection
        )

        # Without an external_account_id, an EnkiAPI cannot be instantiated.
        assert_raises_regexp(
            CannotLoadConfiguration,
            "Enki configuration is incomplete.",
            EnkiAPI,
            self._db,
            self.collection
        )

        self.collection.external_account_id = "1"
        EnkiAPI(self._db, self.collection)

    def test_external_integration(self):
        integration = self.api.external_integration(self._db)
        eq_(ExternalIntegration.ENKI, integration.protocol)

    def test_collection(self):
        eq_(self.collection, self.api.collection)

    def test__run_self_tests(self):
        # Mock every method that will be called by the self-test.
        class Mock(MockEnkiAPI):
            def recent_activity(self, minutes):
                self.recent_activity_called_with = minutes
                yield 1
                yield 2

            def updated_titles(self, minutes):
                self.updated_titles_called_with = minutes
                yield 1
                yield 2
                yield 3

            patron_activity_called_with = []
            def patron_activity(self, patron, pin):
                self.patron_activity_called_with.append((patron, pin))
                yield 1

        api = Mock(self._db)

        # Now let's make sure two Libraries have access to the
        # Collection used in the API -- one library with a default
        # patron and one without.
        no_default_patron = self._library()
        api.collection.libraries.append(no_default_patron)

        with_default_patron = self._default_library
        integration = self._external_integration(
            "api.simple_authentication",
            ExternalIntegration.PATRON_AUTH_GOAL,
            libraries=[with_default_patron]
        )
        p = BasicAuthenticationProvider
        integration.setting(p.TEST_IDENTIFIER).value = "username1"
        integration.setting(p.TEST_PASSWORD).value = "password1"

        # Now that everything is set up, run the self-test.
        no_patron_activity, default_patron_activity, circulation_changes, collection_changes = sorted(
            api._run_self_tests(self._db),
            key=lambda x: x.name
        )

        # Verify that each test method was called and returned the
        # expected SelfTestResult object.
        eq_(60, api.recent_activity_called_with)
        eq_(True, circulation_changes.success)
        eq_("2 circulation events in the last hour", circulation_changes.result)

        eq_(1440, api.updated_titles_called_with)
        eq_(True, collection_changes.success)
        eq_("3 titles added/updated in the last day", collection_changes.result)

        eq_(
            "Acquiring test patron credentials for library %s" % no_default_patron.name,
            no_patron_activity.name
        )
        eq_(False, no_patron_activity.success)
        eq_("Library has no test patron configured.",
            no_patron_activity.exception.message)

        eq_(
            "Checking patron activity, using test patron for library %s" % with_default_patron.name,
            default_patron_activity.name
        )
        eq_(True, default_patron_activity.success)
        eq_("Total loans and holds: 1", default_patron_activity.result)


    def test_request_retried_once(self):
        """A request that times out is retried."""
        class TimesOutOnce(EnkiAPI):
            timeout = True
            called_with = []
            def _request(self, *args, **kwargs):
                self.called_with.append((args, kwargs))
                if self.timeout:
                    self.timeout = False
                    raise RequestTimedOut("url", "timeout")
                else:
                    return MockRequestsResponse(200, content="content")

        api = TimesOutOnce(self._db, self.collection)
        response = api.request("url")

        # Two identical requests were made.
        r1, r2 = api.called_with
        eq_(r1, r2)

        # The timeout was 90 seconds.
        args, kwargs = r1
        eq_(90, kwargs['timeout'])
        eq_(None, kwargs['disallowed_response_codes'])

        # In the end, we got our content.
        eq_(200, response.status_code)
        eq_("content", response.content)

    def test_request_retried_only_once(self):
        """A request that times out twice is not retried."""
        class TimesOut(EnkiAPI):
            calls = 0
            def _request(self, *args, **kwargs):
                self.calls += 1
                raise RequestTimedOut("url", "timeout")

        api = TimesOut(self._db, self.collection)
        assert_raises(RequestTimedOut, api.request, "url")

        # Only two requests were made.
        eq_(2, api.calls)

    def test__minutes_since(self):
        """Test the _minutes_since helper method."""
        an_hour_ago = datetime.datetime.utcnow() - datetime.timedelta(
            minutes=60
        )
        eq_(60, EnkiAPI._minutes_since(an_hour_ago))

    def test_recent_activity(self):
        one_minute_ago = datetime.datetime.utcnow() - datetime.timedelta(
            minutes=1
        )
        data = self.get_data("get_recent_activity.json")
        self.api.queue_response(200, content=data)
        activity = list(self.api.recent_activity(one_minute_ago))
        eq_(43, len(activity))
        for i in activity:
            assert isinstance(i, CirculationData)
        [method, url, headers, data, params, kwargs] = self.api.requests.pop()
        eq_("get", method)
        eq_("https://enkilibrary.org/API/ItemAPI", url)
        eq_("getRecentActivity", params['method'])
        eq_("c", params['lib'])
        eq_(1, params['minutes'])

    def test_updated_titles(self):
        one_minute_ago = datetime.datetime.utcnow() - datetime.timedelta(
            minutes=1
        )
        data = self.get_data("get_update_titles.json")
        self.api.queue_response(200, content=data)
        activity = list(self.api.updated_titles(since=one_minute_ago))

        eq_(6, len(activity))
        for i in activity:
            assert isinstance(i, Metadata)

        eq_("Nervous System", activity[0].title)
        eq_(1, activity[0].circulation.licenses_owned)

        [method, url, headers, data, params, kwargs] = self.api.requests.pop()
        eq_("get", method)
        eq_("https://enkilibrary.org/API/ListAPI", url)
        eq_("getUpdateTitles", params['method'])
        eq_("c", params['lib'])
        eq_(1, params['minutes'])    

    def test_get_item(self):
        data = self.get_data("get_item_french_title.json")
        self.api.queue_response(200, content=data)
        metadata = self.api.get_item("an id")

        # We get a Metadata with associated CirculationData.
        assert isinstance(metadata, Metadata)
        eq_("Le But est le Seul Choix", metadata.title)
        eq_(1, metadata.circulation.licenses_owned)
        
        [method, url, headers, data, params, kwargs] = self.api.requests.pop()
        eq_("get", method)
        eq_("https://enkilibrary.org/API/ItemAPI", url)
        eq_("an id", params["recordid"])
        eq_("getItem", params["method"])
        eq_("c", params['lib'])

        # We asked for a large cover image in case this Metadata is
        # to be used to form a local picture of the book's metadata.
        eq_("large", params['size'])

    def test_get_item_not_found(self):
        self.api.queue_response(200, content="<html>No such book</html>")
        metadata = self.api.get_item("an id")
        eq_(None, metadata)

    def test_get_all_titles(self):
        # get_all_titles and get_update_titles return data in the same
        # format, so we can use one to mock the other.
        data = self.get_data("get_update_titles.json")        
        self.api.queue_response(200, content=data)

        results = list(self.api.get_all_titles())
        eq_(6, len(results))
        metadata = results[0]
        assert isinstance(metadata, Metadata)
        eq_("Nervous System", metadata.title)
        eq_(1, metadata.circulation.licenses_owned)

        [method, url, headers, data, params, kwargs] = self.api.requests.pop()
        eq_("get", method)
        eq_("https://enkilibrary.org/API/ListAPI", url)
        eq_("c", params['lib'])
        eq_("getAllTitles", params['method'])
        eq_("secontent", params['id'])
        
    def test__epoch_to_struct(self):
        """Test the _epoch_to_struct helper method."""
        eq_(datetime.datetime(1970, 1, 1), EnkiAPI._epoch_to_struct("0"))

    def test_checkout_open_access(self):
        """Test that checkout info for non-ACS Enki books is parsed correctly."""
        data = self.get_data("checked_out_direct.json")
        self.api.queue_response(200, content=data)
        result = json.loads(data)
        loan = self.api.parse_patron_loans(result['result']['checkedOutItems'][0])
        eq_(loan.data_source_name, DataSource.ENKI)
        eq_(loan.identifier_type, Identifier.ENKI_ID)
        eq_(loan.identifier, "econtentRecord2")
        eq_(loan.start_date, datetime.datetime(2017, 8, 23, 19, 31, 58, 0))
        eq_(loan.end_date, datetime.datetime(2017, 9, 13, 19, 31, 58, 0))

    def test_checkout_acs(self):
        """Test that checkout info for ACS Enki books is parsed correctly."""
        data = self.get_data("checked_out_acs.json")
        self.api.queue_response(200, content=data)
        result = json.loads(data)
        loan = self.api.parse_patron_loans(result['result']['checkedOutItems'][0])
        eq_(loan.data_source_name, DataSource.ENKI)
        eq_(loan.identifier_type, Identifier.ENKI_ID)
        eq_(loan.identifier, "econtentRecord3334")
        eq_(loan.start_date, datetime.datetime(2017, 8, 23, 19, 42, 35, 0))
        eq_(loan.end_date, datetime.datetime(2017, 9, 13, 19, 42, 35, 0))

    @raises(AuthorizationFailedException)
    def test_checkout_bad_authorization(self):
        """Test that the correct exception is thrown upon an unsuccessful login."""
        data = self.get_data("login_unsuccessful.json")
        self.api.queue_response(200, content=data)
        result = json.loads(data)

        edition, pool = self._edition(
            identifier_type=Identifier.ENKI_ID,
            data_source_name=DataSource.ENKI,
            with_license_pool=True
        )
        pool.identifier.identifier = 'notanid'

        patron = self._patron(external_identifier='notabarcode')

        loan = self.api.checkout(patron,'notapin',pool,None)

    @raises(NoAvailableCopies)
    def test_checkout_not_available(self):
        """Test that the correct exception is thrown upon an unsuccessful login."""
        data = self.get_data("no_copies.json")
        self.api.queue_response(200, content=data)
        result = json.loads(data)

        edition, pool = self._edition(
            identifier_type=Identifier.ENKI_ID,
            data_source_name=DataSource.ENKI,
            with_license_pool=True
        )
        pool.identifier.identifier = 'econtentRecord1'
        patron = self._patron(external_identifier='12345678901234')

        loan = self.api.checkout(patron,'1234',pool,None)

    def test_fulfillment_open_access(self):
        """Test that fulfillment info for non-ACS Enki books is parsed correctly."""
        data = self.get_data("checked_out_direct.json")
        self.api.queue_response(200, content=data)
        result = json.loads(data)
        fulfill_data = self.api.parse_fulfill_result(result['result'])
        eq_(fulfill_data[0], """http://cccl.enkilibrary.org/API/UserAPI?method=downloadEContentFile&username=21901000008080&password=deng&lib=1&recordId=2""")
        eq_(fulfill_data[1], 'epub')

    def test_fulfillment_acs(self):
        """Test that fulfillment info for ACS Enki books is parsed correctly."""
        data = self.get_data("checked_out_acs.json")
        self.api.queue_response(200, content=data)
        result = json.loads(data)
        fulfill_data = self.api.parse_fulfill_result(result['result'])
        eq_(fulfill_data[0], """http://afs.enkilibrary.org/fulfillment/URLLink.acsm?action=enterloan&ordersource=Califa&orderid=ACS4-9243146841581187248119581&resid=urn%3Auuid%3Ad5f54da9-8177-43de-a53d-ef521bc113b4&gbauthdate=Wed%2C+23+Aug+2017+19%3A42%3A35+%2B0000&dateval=1503517355&rights=%24lat%231505331755%24&gblver=4&auth=8604f0fc3f014365ea8d3c4198c721ed7ed2c16d""")
        eq_(fulfill_data[1], 'epub')

    def test_patron_activity(self):
        data = self.get_data("patron_response.json")
        self.api.queue_response(200, content=data)
        patron = self._patron()
        [loan] = self.api.patron_activity(patron, 'pin')
        assert isinstance(loan, LoanInfo)

        eq_(Identifier.ENKI_ID, loan.identifier_type)
        eq_(DataSource.ENKI, loan.data_source_name)
        eq_("econtentRecord231", loan.identifier)
        eq_(self.collection, loan.collection(self._db))
        eq_(datetime.datetime(2017, 8, 15, 14, 56, 51), loan.start_date)
        eq_(datetime.datetime(2017, 9, 5, 14, 56, 51), loan.end_date)

    def test_patron_activity_failure(self):
        patron = self._patron()
        self.api.queue_response(404, "No such patron")
        collect = lambda: list(self.api.patron_activity(patron, 'pin'))
        assert_raises(PatronNotFoundOnRemote, collect)

        msg = dict(result=dict(message="Login unsuccessful."))
        self.api.queue_response(200, content=json.dumps(msg))
        assert_raises(AuthorizationFailedException, collect)

        msg = dict(result=dict(message="Some other error."))
        self.api.queue_response(200, content=json.dumps(msg))
        assert_raises(CirculationException, collect)

class TestBibliographicParser(object):
    """TODO"""


class TestEnkiImport(BaseEnkiTest):
    """TODO starting here"""

    def test_import_instantiation(self):
        """Test that EnkiImport can be instantiated"""
        importer = EnkiImport(self._db, self.collection, api_class=self.api)
        eq_(self.api, importer.api)


class TestEnkiCollectionReaper(BaseEnkiTest):

    def test_reaped_book_has_zero_licenses(self):
        data = "<html></html>"

        # Create a LicensePool that needs updating.
        edition, pool = self._edition(
            identifier_type=Identifier.ENKI_ID,
            data_source_name=DataSource.ENKI,
            with_license_pool=True
        )

        # This is a specific record ID that should never exist
        nonexistent_id = "econtentRecord0"

        # We have never checked the circulation information for this
        # LicensePool. Put some random junk in the pool to verify
        # that it gets changed.
        pool.licenses_owned = 10
        pool.licenses_available = 5
        pool.patrons_in_hold_queue = 3
        pool.identifier.identifier = nonexistent_id
        eq_(None, pool.last_checked)

        # Modify the data so that it appears to be talking about the
        # book we just created.

        self.api.queue_response(200, content=data)

        circulationdata = self.api.reaper_request(pool.identifier)

        eq_(0, circulationdata.licenses_owned)
        eq_(0, circulationdata.licenses_available)
        eq_(0, circulationdata.patrons_in_hold_queue)

