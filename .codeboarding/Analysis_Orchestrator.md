```mermaid
graph LR
    Workflow_Lifecycle_Controller["Workflow Lifecycle Controller"]
    Strategy_State_Manager["Strategy & State Manager"]
    Integrity_Health_Guard["Integrity & Health Guard"]
    Workflow_Lifecycle_Controller -- "orchestrates analysis mode and execution" --> Strategy_State_Manager
    Workflow_Lifecycle_Controller -- "triggers system health and baseline validation" --> Integrity_Health_Guard
    Strategy_State_Manager -- "provides configuration parameters for depth and scope" --> Workflow_Lifecycle_Controller
    Strategy_State_Manager -- "validates baseline integrity during state transitions" --> Integrity_Health_Guard
```

[![CodeBoarding](https://img.shields.io/badge/Generated%20by-CodeBoarding-9cf?style=flat-square)](https://github.com/CodeBoarding/CodeBoarding)[![Demo](https://img.shields.io/badge/Try%20our-Demo-blue?style=flat-square)](https://www.codeboarding.org/diagrams)[![Contact](https://img.shields.io/badge/Contact%20us%20-%20contact@codeboarding.org-lightgrey?style=flat-square)](mailto:contact@codeboarding.org)

## Details

Acts as the central controller for the action. It validates the environment, manages the transition between full codebase scans and incremental PR analysis, and ensures metadata consistency across runs.

### Workflow Lifecycle Controller
Manages the high-level execution flow of the action, including initialization, environment setup, and the sequencing of analysis tasks. It incorporates run_seed, run_concat, and quota management logic to handle the full lifecycle of the analysis process.


**Related Classes/Methods**:

- `scripts.engine_adapter.main`:523-601
- `scripts.engine_adapter.run_head`:338-359
- `scripts.engine_adapter._is_quota_exhausted`:69-84



**Source Files:**

- [`scripts/engine_adapter.py`](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py)
  - `scripts.engine_adapter._is_quota_exhausted` ([L69-L84](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L69-L84)) - Function
  - `scripts.engine_adapter._flag_quota_exhausted` ([L87-L96](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L87-L96)) - Function
  - `scripts.engine_adapter._log_path` ([L99-L100](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L99-L100)) - Function
  - `scripts.engine_adapter._clear_dir` ([L103-L109](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L103-L109)) - Function
  - `scripts.engine_adapter._load_metadata` ([L112-L122](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L112-L122)) - Function
  - `scripts.engine_adapter._metadata_depth` ([L125-L129](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L125-L129)) - Function
  - `scripts.engine_adapter._metadata_commit` ([L132-L134](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L132-L134)) - Function
  - `scripts.engine_adapter.baseline_info` ([L137-L147](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L137-L147)) - Function
  - `scripts.engine_adapter.validate_base_analysis` ([L184-L239](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L184-L239)) - Function
  - `scripts.engine_adapter.run_base` ([L242-L254](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L242-L254)) - Function
  - `scripts.engine_adapter.run_seed` ([L257-L284](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L257-L284)) - Function
  - `scripts.engine_adapter._incremental_or_full` ([L287-L335](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L287-L335)) - Function
  - `scripts.engine_adapter.run_head` ([L338-L359](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L338-L359)) - Function
  - `scripts.engine_adapter.run_analyze` ([L362-L429](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L362-L429)) - Function
  - `scripts.engine_adapter.run_analyze.full` ([L378-L394](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L378-L394)) - Function
  - `scripts.engine_adapter.run_render` ([L432-L445](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L432-L445)) - Function
  - `scripts.engine_adapter.run_concat` ([L448-L461](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L448-L461)) - Function
  - `scripts.engine_adapter.main` ([L523-L601](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L523-L601)) - Function


### Strategy & State Manager
The decision-making engine that determines the analysis mode (Full vs. Incremental). It tracks commit hashes and metadata to ensure that PR analysis only processes changed files, maintaining consistency with the established baseline.


**Related Classes/Methods**:

- `scripts.engine_adapter.run_analyze`:362-429
- `scripts.engine_adapter._incremental_or_full`:287-335
- `scripts.engine_adapter._load_metadata`:112-122
- `scripts.engine_adapter.run_analyze.full`:378-394



**Source Files:**

- [`scripts/engine_adapter.py`](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py)
  - `scripts.engine_adapter._log_path` ([L99-L100](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L99-L100)) - Function
  - `scripts.engine_adapter._clear_dir` ([L103-L109](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L103-L109)) - Function
  - `scripts.engine_adapter._load_metadata` ([L112-L122](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L112-L122)) - Function
  - `scripts.engine_adapter._metadata_commit` ([L132-L134](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L132-L134)) - Function
  - `scripts.engine_adapter.baseline_info` ([L137-L147](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L137-L147)) - Function
  - `scripts.engine_adapter.run_base` ([L242-L254](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L242-L254)) - Function
  - `scripts.engine_adapter._incremental_or_full` ([L287-L335](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L287-L335)) - Function
  - `scripts.engine_adapter.run_analyze` ([L362-L429](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L362-L429)) - Function
  - `scripts.engine_adapter.run_analyze.full` ([L378-L394](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L378-L394)) - Function
  - `scripts.engine_adapter.run_render` ([L432-L445](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L432-L445)) - Function


### Integrity & Health Guard
Monitors the health of the analysis process, validates the integrity of the baseline data, and reports on issues or quota exhaustion. This component implements health reporting and issue counting logic to ensure system stability.


**Related Classes/Methods**:

- `scripts.engine_adapter.validate_base_analysis`:184-239
- `scripts.engine_adapter._docs_only_baseline_drift`:150-181
- `scripts.engine_adapter.run_health`:491-520



**Source Files:**

- [`scripts/build_component_files.py`](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_component_files.py)
  - `scripts.build_component_files._block` ([L104-L121](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_component_files.py#L104-L121)) - Function
  - `scripts.build_component_files.render_component_files` ([L124-L175](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_component_files.py#L124-L175)) - Function
- [`scripts/diff_to_mermaid.py`](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py)
  - `scripts.diff_to_mermaid._comp_id` ([L60-L61](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L60-L61)) - Function
  - `scripts.diff_to_mermaid._comp_name` ([L64-L65](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L64-L65)) - Function
  - `scripts.diff_to_mermaid._has_changes` ([L151-L159](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L151-L159)) - Function
  - `scripts.diff_to_mermaid._sanitize` ([L223-L225](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L223-L225)) - Function
  - `scripts.diff_to_mermaid._esc` ([L247-L253](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L247-L253)) - Function
  - `scripts.diff_to_mermaid._truncate` ([L256-L258](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L256-L258)) - Function
  - `scripts.diff_to_mermaid._display_status` ([L261-L262](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L261-L262)) - Function
  - `scripts.diff_to_mermaid._Scope` ([L265-L309](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L265-L309)) - Class
  - `scripts.diff_to_mermaid._Scope.__init__` ([L276-L298](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L276-L298)) - Method
  - `scripts.diff_to_mermaid._filter_changed` ([L312-L354](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L312-L354)) - Function
  - `scripts.diff_to_mermaid._filter_changed.touches` ([L345-L347](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L345-L347)) - Function
  - `scripts.diff_to_mermaid._init_directive` ([L357-L376](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L357-L376)) - Function
  - `scripts.diff_to_mermaid._count_changed_components` ([L379-L386](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L379-L386)) - Function
  - `scripts.diff_to_mermaid._has_changed_relations` ([L389-L393](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L389-L393)) - Function
  - `scripts.diff_to_mermaid.render_mermaid` ([L396-L521](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L396-L521)) - Function
  - `scripts.diff_to_mermaid.render_mermaid.build` ([L424-L497](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L424-L497)) - Function
  - `scripts.diff_to_mermaid.render_mermaid.build.emit_edges` ([L435-L447](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L435-L447)) - Function
  - `scripts.diff_to_mermaid.render_mermaid.build.emit_level` ([L449-L466](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L449-L466)) - Function
- [`scripts/engine_adapter.py`](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py)
  - `scripts.engine_adapter._count_report_issues` ([L464-L477](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L464-L477)) - Function
  - `scripts.engine_adapter._count_health_report` ([L480-L488](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L480-L488)) - Function
  - `scripts.engine_adapter.run_health` ([L491-L520](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L491-L520)) - Function




### [FAQ](https://github.com/CodeBoarding/GeneratedOnBoardings/tree/main?tab=readme-ov-file#faq)