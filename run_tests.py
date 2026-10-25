"""Run the named offline suite; never discover historical experiment modules."""
import hashlib,json,os,socket,sys,unittest,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def denied(*args,**kwargs):raise AssertionError('Network forbidden in offline tests')
def main():
    os.chdir(ROOT);sys.path.insert(0,str(ROOT/'work'))
    scratch=ROOT/'.test-tmp';scratch.mkdir(exist_ok=True)
    os.environ['TMP']=str(scratch);os.environ['TEMP']=str(scratch)
    import tempfile
    tempfile.tempdir=str(scratch)
    socket.create_connection=denied;socket.socket.connect=denied;urllib.request.urlopen=denied
    names=['test_candidate_service_v1', 'test_competition_adapter_v1', 'test_operations_ledger_v1', 'test_candidate_pipeline_v1', 'test_evidence_quote_spans', 'test_numeric_cdf', 'test_compact_forecast_review_candidate', 'test_candidate_access_probe_v1', 'test_candidate_integrity_v1', 'test_git_state_store_v1', 'test_state_probe_v1', 'test_candidate_runtime_v1', 'test_candidate_dryrun_v1', 'test_fourtype_integration_v2', 'test_cdf_projection_v1', 'test_mc_option_contract_v1', 'test_git_timing_v1', 'test_fourtype_dryrun_v2', 'test_fourtype_parser_probe_v1', 'test_checkpoint_objects_v1', 'test_source_stop_v1', 'test_quality_implementation', 'test_question_archive_v1', 'test_season_storage_v1', 'test_checkpoint_coalescing_v1', 'test_proven_unsent_v1', 'test_classified_review_v1', 'test_integrated_review_probe_v1', 'test_source_fallback_v1', 'test_source_http_v1', 'test_source_alternatives_probe_v1', 'test_question_queue_v1', 'test_argon_fixes_v1', 'test_context_excerpt_candidate', 'test_bounded_evidence', 'test_argon_followup_v1', 'test_broad_audit_v1', 'test_broad_followup_v1', 'test_fable_final_v1', 'test_test_area_acceptance_v1', 'test_acceptance_runtime_v1', 'test_comment_contract_v2', 'test_submission_unsent_v1', 'test_candidate_cost_v1', 'test_recovery_intake_v1', 'test_production_cycle_v1', 'test_publication_intent_v1', 'test_production_adapters_v1', 'test_publication_entry_v1', 'test_launch_review_v2', 'test_question_source_links_v1', 'test_text_source_capture', 'test_numeric_diagnostics_v1', 'test_outcome_metrics_v1', 'test_need_gap_classification_v1']
    suite=unittest.defaultTestLoader.loadTestsFromNames(names)
    expected=suite.countTestCases()
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    receipt=dict(tests=result.testsRun,expected_tests=expected,failures=len(result.failures),errors=len(result.errors),
                 network='blocked',api_generations=0,submissions=0,
                 manifest_sha256=hashlib.sha256((ROOT/'SOURCE_MANIFEST.json').read_bytes()).hexdigest())
    (scratch/'offline-test-result.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(receipt))
    return 0 if result.wasSuccessful() and result.testsRun==expected else 1
if __name__=='__main__':raise SystemExit(main())
