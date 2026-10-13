import unittest

from auto_lammps.tasks import TaskError
from auto_lammps.workbench_bridge import _reference_paper_sources


class ReferencePaperSourcesTests(unittest.TestCase):
    def test_normal_paper_retains_each_whole_page_and_locator(self):
        pages=['First full page', 'Second full page', 'Third full page']
        rows=_reference_paper_sources(pages)
        self.assertEqual([r['text'] for r in rows], pages)
        self.assertEqual([r['locator'] for r in rows], ['PDF page 1','PDF page 2','PDF page 3'])

    def test_long_paper_joins_whole_contiguous_pages_without_omission(self):
        pages=[f'Whole page {i:03d} '+('source '+str(i)+'\n')*180 for i in range(1,53)]
        rows=_reference_paper_sources(pages)
        self.assertLessEqual(len(rows),32)
        self.assertEqual('\n\n'.join(r['text'] for r in rows), '\n\n'.join(page.strip() for page in pages))
        self.assertTrue(all(len(r['text'])<=24000 for r in rows))
        self.assertTrue(all(r['locator'].startswith('PDF pages ') for r in rows))

    def test_single_page_overflow_is_explicit_not_truncated(self):
        with self.assertRaisesRegex(TaskError,'单页完整原文'):
            _reference_paper_sources(['x'*24001])

    def test_total_capacity_overflow_cannot_publish_partial_sources(self):
        with self.assertRaises(TaskError):
            _reference_paper_sources(['x'*23000 for _ in range(52)])

    def test_undecodable_glyph_remains_unknown_and_source_declares_conversion(self):
        rows=_reference_paper_sources(['First page unchanged','Energy E\x00 remains undecoded'])
        self.assertEqual(rows[0]['text'],'First page unchanged')
        self.assertEqual(rows[1]['text'],'Energy E\uFFFD remains undecoded')
        self.assertIn('1 undecodable NUL glyph',rows[1]['locator'])
        self.assertIn('original PDF retained',rows[1]['locator'])


if __name__=='__main__':unittest.main()
