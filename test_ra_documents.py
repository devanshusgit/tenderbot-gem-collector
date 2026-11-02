"""Standalone offline contracts for the observed GeM reverse auction format.

The excerpts reproduce document labels and non-personal procurement fields.
Extraction is mocked; no PDF files, source requests or private fixtures are used.
"""
from copy import deepcopy
import unittest
from unittest import mock

from tenderbot_crawler.documents import DOCUMENT_PARSER_VERSION, DocumentMismatchError, parse_pdf


FETCHED_AT = '2026-11-02T14:46:13.600398+00:00'
RA_URL = 'https://bidplus.gem.gov.in/showradocumentPdf/10111405'
BID_URL = 'https://bidplus.gem.gov.in/showbidDocument/9984290'
RECORD = {'id':'gem-10111405','tender_id':'GEM/2026/R/762937',
    'source_document_url':RA_URL,'closing_at':'2026-11-03T19:00:00+05:30'}
AUTO_EXTENSION = 'If any seller submits the offer price during last 15 minutes of RA end time, then RA will be automatically extended for next 15 minutes from the last participation time. There is no limit on such RA extension cycle.'


def extraction():
    return {
        'pages':[
            'RA Number: GEM/2026/R/762937\nRA Document\nRA Details\n'
            'RA Start Date/Time 02-11-2026 19:00:00\nRA End Date/Time 03-11-2026 19:00:00\n'
            'Total Quantity 1',
            'Please refer to Bid document of GEM/2026/B/8116159 for technical specification, terms &\nconditions and other details.',
        ],
        'tables':[{'page':1,'tables':[[
            ['RA Details',None],
            ['RA Start Date/Time','02-11-2026 19:00:00'],
            ['RA End Date/Time','03-11-2026 19:00:00'],
            ['Ministry/State Name','Example public authority'],
            ['Department Name','Example department'],
            ['Organisation Name','Example organisation'],
            ['Office Name','***********'],
            ['Total Quantity','1'],
            ['Item Category','High Mast Lighting Tower for large area with LED\nFlood Lighting System'],
            ['Auto Extension',AUTO_EXTENSION],
            ['Minimum Average Annual Turnover of the bidder (For the last 3 Financial Years)','4 Lakh (s)'],
            ['MSE Exemption for Years Of Experience and Turnover','Yes | Complete'],
            ['Startup Exemption for Years Of Experience and Turnover','Yes | Complete'],
        ]]},{'page':2,'tables':[]}],
        'links':[{'url':BID_URL,'page':2,'label':'GEM/2026/B/8116159'},
            {'url':'https://admin.gem.gov.in/apis/v1/gtc/pdfByDate/?date=20261101','page':2,'label':'Download'}],
    }


class ReverseAuctionPDFTests(unittest.TestCase):
    def parse(self,record=None,extracted=None,**kwargs):
        with mock.patch('tenderbot_crawler.documents._read_pdf',
                return_value=('a'*64,deepcopy(extracted if extracted is not None else extraction()))), \
                mock.patch('socket.create_connection',side_effect=AssertionError('Parser must stay offline')):
            return parse_pdf('unused-offline.pdf',RECORD if record is None else record,FETCHED_AT,**kwargs)

    def test_observed_ra_identity_dates_quantity_and_requirements_are_parsed(self):
        result = self.parse()
        self.assertEqual(result['detail_status'],'parsed')
        self.assertEqual(result['tender_id'],RECORD['tender_id'])
        self.assertEqual(result['ra_start_at'],'2026-11-02T19:00:00+05:30')
        self.assertEqual(result['closing_at'],'2026-11-03T19:00:00+05:30')
        self.assertIsNone(result['opening_at'],'Auction start is not a bid-opening date')
        self.assertEqual(result['quantity'],1)
        self.assertEqual(result['bidder_turnover_lakhs'],4)
        self.assertEqual(result['auto_extension'],AUTO_EXTENSION)
        self.assertIn('MSE Exemption',result['mse_exemption'])
        self.assertIn('Yes | Complete',result['startup_exemption'])
        self.assertTrue(any('MSE Exemption' in text for text in result['eligibility']))
        self.assertIsNone(result['office'])
        self.assertIsNone(result['emd_fee'])
        self.assertIsNone(result['tender_value'])
        self.assertIn('High Mast Lighting Tower',result['description'])
        self.assertEqual(result['document_parser_version'],DOCUMENT_PARSER_VERSION)
        self.assertEqual(DOCUMENT_PARSER_VERSION,2)

    def test_ra_parent_bid_is_a_recorded_reference_not_its_identity_or_terms(self):
        result = self.parse()
        self.assertEqual(result['originating_bid_reference'],'GEM/2026/B/8116159')
        self.assertEqual(result['tender_id'],'GEM/2026/R/762937')
        self.assertEqual(result['technical_specifications'],[])
        self.assertEqual(result['buyer_added_terms'],[])
        self.assertIn('originating bid',result['detail_warning'])
        self.assertIn(BID_URL,[item['url'] for item in result['documents']])
        self.assertEqual(result['documents'][0]['name'],'Original GeM reverse auction document')
        parent = next(item for item in result['evidence'] if item['field'] == 'originating_bid_reference')
        self.assertEqual(parent['page'],2)
        self.assertEqual(parent['source_url'],RA_URL)

    def test_all_extracted_field_evidence_belongs_to_actual_ra_pages_and_url(self):
        result = self.parse()
        self.assertTrue(all(item['source_url'] == RA_URL for item in result['evidence']))
        self.assertTrue(all(item['page'] in (1,2) for item in result['evidence']))
        identity = next(item for item in result['evidence'] if item['field'] == 'tender_id')
        self.assertEqual(identity['quote'],'RA Number: GEM/2026/R/762937')
        self.assertEqual(result['source_document_fetched_at'],FETCHED_AT)
        self.assertEqual(result['document_sha256'],'a'*64)

    def test_different_ra_reference_raises_before_attaching_requirements(self):
        with self.assertRaises(DocumentMismatchError) as mismatch:
            self.parse({**RECORD,'tender_id':'GEM/2026/R/762938'})
        self.assertEqual(mismatch.exception.expected,'GEM/2026/R/762938')
        self.assertEqual(mismatch.exception.observed,'GEM/2026/R/762937')

    def test_parent_bid_match_cannot_override_mismatched_ra_document(self):
        with self.assertRaises(DocumentMismatchError) as mismatch:
            self.parse({**RECORD,'tender_id':'GEM/2026/B/8116159'})
        self.assertEqual(mismatch.exception.observed,'GEM/2026/R/762937')

    def test_ra_reference_requires_ra_label_and_strict_numeric_reference(self):
        for header in ('Bid Number: GEM/2026/R/762937','RA Number: GEM/2026/B/8116159',
                'RA Number: GEM/2026/R/not-numeric','RA Number: GEM/2026/R/762937/extra',
                'RA Number: GEM/2026/R/762937suffix'):
            extracted = extraction()
            extracted['pages'][0] = header
            extracted['pages'][1] = ''
            with self.subTest(header=header):
                result = self.parse(extracted=extracted)
                self.assertEqual(result['detail_status'],'unrecognized_document')
                self.assertIsNone(result['tender_id'])
                self.assertIsNone(result['quantity'])
                self.assertEqual(result['evidence'],[])

    def test_unsupported_text_does_not_acquire_ra_table_fields(self):
        extracted = extraction()
        extracted['pages'][0] = 'Reverse auction information without a labelled RA identity'
        result = self.parse(extracted=extracted)
        self.assertEqual(result['detail_status'],'unrecognized_document')
        self.assertIsNone(result['ra_start_at'])
        self.assertIsNone(result['closing_at'])
        self.assertIsNone(result['quantity'])
        self.assertIsNone(result['originating_bid_reference'])
        self.assertEqual(result['document_parser_version'],2)

    def test_current_listing_deadline_wins_over_stale_ra_end_time(self):
        current = '2026-11-05T19:00:00+05:30'
        result = self.parse({**RECORD,'closing_at':current})
        self.assertEqual(result['closing_at'],current)
        self.assertEqual(result['document_closing_at'],'2026-11-03T19:00:00+05:30')
        self.assertIsNone(result['opening_at'])
        self.assertFalse(any(item['field'] == 'closing_at' for item in result['evidence']))
        original = next(item for item in result['evidence'] if item['field'] == 'document_closing_at')
        self.assertEqual(original['source_url'],RA_URL)
        self.assertIn('listing closing date is retained',result['detail_warning'])

    def test_existing_bid_identity_and_bid_dates_still_parse(self):
        extracted = {'pages':['Bid Number: GEM/2026/B/1234567'],'links':[],
            'tables':[{'page':1,'tables':[[
                ['Bid End Date/Time','03-11-2026 19:00:00'],
                ['Bid Opening Date/Time','04-11-2026 19:00:00'],
            ]]}]}
        result = self.parse({'id':'gem-1234567','tender_id':'GEM/2026/B/1234567'},extracted)
        self.assertEqual(result['detail_status'],'parsed')
        self.assertEqual(result['closing_at'],'2026-11-03T19:00:00+05:30')
        self.assertEqual(result['opening_at'],'2026-11-04T19:00:00+05:30')
        self.assertIsNone(result['ra_start_at'])
        self.assertIsNone(result['originating_bid_reference'])
        self.assertEqual(result['document_parser_version'],2)

    def test_blank_pdf_outcome_still_records_parser_version_without_fields(self):
        result = self.parse(extracted={'pages':[''],'tables':[],'links':[]})
        self.assertEqual(result['detail_status'],'no_readable_text')
        self.assertEqual(result['document_parser_version'],2)
        self.assertIsNone(result['tender_id'])
        self.assertIsNone(result['quantity'])

    def test_explicit_source_url_and_arbitrary_local_id_are_preserved(self):
        source = 'https://bidplus.gem.gov.in/showradocumentPdf/1111111'
        result = self.parse({**RECORD,'id':'local-source-document'},source_url=source)
        self.assertEqual(result['id'],'local-source-document')
        self.assertEqual(result['source_document_url'],source)
        self.assertEqual(result['documents'][0]['url'],source)
        self.assertTrue(all(item['source_url'] == source for item in result['evidence']))


if __name__ == '__main__':
    unittest.main()
