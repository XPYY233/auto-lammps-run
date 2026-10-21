"""Whole original text coverage from byte-pinned, separately retained pages."""
from copy import deepcopy
import unittest

from auto_lammps.author_reference_plan import author_source_text_coverage, author_source_inputs, _source
from auto_lammps.manifest import canonical, sha256
from auto_lammps.tasks import TaskError
import test_author_source_reader as reader_fixture


def evidence(lines=421,windows=((1,400),(401,421))):
    text=''.join('# Synthetic original line '+str(index)+'\n' for index in range(1,lines+1))
    raw=text.encode();file=dict(path='original.py',size=len(raw),sha256=sha256(raw))
    rows=text.splitlines(keepends=True);reads=[];sources=[]
    for index,(start,end) in enumerate(windows):
        returned=min(end,lines);fragment=''.join(rows[start-1:end])
        identity=dict(path=file['path'],sha256=file['sha256'],start_line=start,
            returned_end_line=returned,text_sha256=sha256(fragment.encode()))
        source_id='author-'+sha256(canonical(identity))[:32]
        sources.append(_source(source_id,'author_source',file['path']+':'+str(start)+'-'+str(returned),fragment))
        reads.append(dict(file,source_id=source_id,request_sha256=str(index+1)*64,state='read',
            start_line=start,end_line=end,returned_end_line=returned,total_lines=lines,
            truncated=start>1 or end<lines,text_sha256=sha256(fragment.encode())))
    return dict(files=[file],reads=reads,sources=sources)


class AuthorSourceCoverageTests(unittest.TestCase):
    def package(self,index,size):
        fx=reader_fixture.AuthorSourceReaderTests();fx.setUp();self.addCleanup(fx.doCleanups)
        path='original-'+str(index)+'.py';fx.texts={path:b'#'+bytes([65+index])*(size-2)+b'\n'}
        fx._write_sources();fx._bind_receipt()
        request=fx.request([dict(path=path,start_line=1,end_line=400)])
        return dict(capability=fx.service().capability(fx.receipt,purpose='author_reference'),
            acquisition_receipt=deepcopy(fx.receipt),reads=[dict(request=request,response=fx.worker(request))])

    def test_three_independently_valid_packages_fit_fixed_collection_cap_above_reader_cap(self):
        packages=[self.package(index,100000) for index in range(3)]
        checked=author_source_inputs(dict(source_packages=packages),'b'*32)
        self.assertEqual(sum(len(row['text'].encode()) for row in checked['sources']),300000)
        self.assertEqual(len(author_source_text_coverage(checked)['complete_files']),3)

    def test_individually_valid_packages_above_fixed_512_kib_collection_cap_are_rejected(self):
        packages=[self.package(index,180000) for index in range(3)]
        with self.assertRaisesRegex(TaskError,'512 KiB'):
            author_source_inputs(dict(source_packages=packages),'b'*32)
    def test_421_lines_preserve_both_partial_receipts_and_prove_whole_original_sha(self):
        checked=evidence();old=deepcopy(checked)
        result=author_source_text_coverage(checked)
        self.assertEqual(len(result['complete_files']),1);self.assertEqual(result['incomplete_files'],[])
        proof=result['complete_files'][0]
        self.assertEqual(proof['sha256'],checked['files'][0]['sha256']);self.assertEqual(proof['total_lines'],421)
        self.assertEqual(proof['fragments'],checked['reads'])
        self.assertTrue(all(row['truncated'] for row in checked['reads']))
        self.assertEqual(checked,old)

    def test_missing_middle_or_tail_is_explicitly_incomplete(self):
        for windows in (((1,400),),((1,300),(302,421)),((401,421),)):
            with self.subTest(windows=windows):
                result=author_source_text_coverage(evidence(windows=windows))
                self.assertEqual(result['complete_files'],[])
                self.assertEqual(result['incomplete_files'][0]['reason'],'missing_contiguous_text')

    def test_overlap_or_mixed_version_size_or_line_count_never_proves_full_text(self):
        with self.assertRaises(TaskError):author_source_text_coverage(evidence(windows=((1,300),(300,421))))
        for key,value in (('sha256','f'*64),('size',1),('total_lines',422)):
            checked=evidence();checked['reads'][1][key]=value
            with self.subTest(key=key),self.assertRaises(TaskError):author_source_text_coverage(checked)

    def test_short_legacy_file_keeps_single_fragment_compatibility(self):
        checked=evidence(lines=12,windows=((1,400),))
        result=author_source_text_coverage(checked)
        self.assertEqual(result['complete_files'][0]['total_lines'],12)
        self.assertFalse(result['complete_files'][0]['fragments'][0]['truncated'])

    def test_changed_text_or_whole_original_digest_and_byte_limit_are_rejected(self):
        checked=evidence();checked['sources'][1]['text']+='changed\n'
        with self.assertRaises(TaskError):author_source_text_coverage(checked)
        checked=evidence()
        for row in checked['files']+checked['reads']:row['sha256']='f'*64
        with self.assertRaises(TaskError):author_source_text_coverage(checked)
        checked=evidence(lines=1,windows=((1,400),));checked['sources'][0]['text']='x'*300000
        with self.assertRaises(TaskError):author_source_text_coverage(checked)


if __name__=='__main__':unittest.main()
