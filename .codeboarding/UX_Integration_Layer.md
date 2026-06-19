```mermaid
graph LR
    Telemetry_Feedback_Handler["Telemetry & Feedback Handler"]
    Presentation_Orchestrator_State_Manager["Presentation Orchestrator & State Manager"]
    Visual_Diagramming_Engine["Visual Diagramming Engine"]
    IDE_Web_Integration_Handler["IDE & Web Integration Handler"]
    Presentation_Orchestrator_State_Manager -- "delegates diagram generation to" --> Visual_Diagramming_Engine
    Presentation_Orchestrator_State_Manager -- "provides analysis state for CTA generation" --> IDE_Web_Integration_Handler
    Presentation_Orchestrator_State_Manager -- "triggers feedback collection on completion" --> Telemetry_Feedback_Handler
    Visual_Diagramming_Engine -- "provides component identifiers for deep-linking" --> IDE_Web_Integration_Handler
    Telemetry_Feedback_Handler -- "requests visual context for feedback reports" --> Visual_Diagramming_Engine
    Data_Adapter_Pre_processor -- "Passes metadata about the analysis to be included in telemetry payloads" --> Telemetry_Feedback_Handler
    Telemetry_Feedback_Handler -- "calls" --> Architectural_Visualization_Engine
```

[![CodeBoarding](https://img.shields.io/badge/Generated%20by-CodeBoarding-9cf?style=flat-square)](https://github.com/CodeBoarding/CodeBoarding)[![Demo](https://img.shields.io/badge/Try%20our-Demo-blue?style=flat-square)](https://www.codeboarding.org/diagrams)[![Contact](https://img.shields.io/badge/Contact%20us%20-%20contact@codeboarding.org-lightgrey?style=flat-square)](mailto:contact@codeboarding.org)

## Details

Manages the final presentation of data to the user, including GitHub comments, feedback loops, and external integrations. It handles telemetry and user feedback via PostHog, closing the feedback loop between the user and the tool. Key class/method: scripts.submit_feedback.py.

### Telemetry & Feedback Handler
Manages the collection and submission of telemetry data and user feedback to external endpoints. Includes logic from scripts.submit_feedback.


**Related Classes/Methods**: _None_


**Source Files:**

- [`scripts/diff_to_mermaid.py`](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py)
  - `scripts.diff_to_mermaid._truncate` ([L256-L258](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L256-L258)) - Function
  - `scripts.diff_to_mermaid.render_mermaid` ([L396-L521](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L396-L521)) - Function
  - `scripts.diff_to_mermaid.render_mermaid.build` ([L424-L497](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L424-L497)) - Function


### Presentation Orchestrator & State Manager
Acts as the primary coordinator for the UX layer, validating analysis state, managing quotas, and ensuring data consistency for visualizers and CTA builders.


**Related Classes/Methods**:

- `scripts.engine_adapter.main`:519-607
- `scripts.engine_adapter.run_analyze`:371-430
- `scripts.engine_adapter.validate_base_analysis`:183-233



**Source Files:**

- [`scripts/engine_adapter.py`](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py)
  - `scripts.engine_adapter._is_quota_exhausted` ([L89-L104](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L89-L104)) - Function
  - `scripts.engine_adapter._flag_quota_exhausted` ([L107-L116](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L107-L116)) - Function
  - `scripts.engine_adapter._log_path` ([L119-L120](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L119-L120)) - Function
  - `scripts.engine_adapter._clear_dir` ([L123-L129](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L123-L129)) - Function
  - `scripts.engine_adapter._load_metadata` ([L132-L142](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L132-L142)) - Function
  - `scripts.engine_adapter._metadata_depth` ([L145-L149](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L145-L149)) - Function
  - `scripts.engine_adapter._supported_depth` ([L152-L154](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L152-L154)) - Function
  - `scripts.engine_adapter._analysis_depth_or_default` ([L157-L162](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L157-L162)) - Function
  - `scripts.engine_adapter._metadata_commit` ([L165-L167](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L165-L167)) - Function
  - `scripts.engine_adapter.baseline_info` ([L170-L180](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L170-L180)) - Function
  - `scripts.engine_adapter.validate_base_analysis` ([L183-L233](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L183-L233)) - Function
  - `scripts.engine_adapter.run_base` ([L236-L246](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L236-L246)) - Function
  - `scripts.engine_adapter.run_seed` ([L249-L272](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L249-L272)) - Function
  - `scripts.engine_adapter._incremental_or_full` ([L275-L322](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L275-L322)) - Function
  - `scripts.engine_adapter.run_head` ([L325-L368](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L325-L368)) - Function
  - `scripts.engine_adapter.run_analyze` ([L371-L430](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L371-L430)) - Function
  - `scripts.engine_adapter.run_analyze.full` ([L387-L401](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L387-L401)) - Function
  - `scripts.engine_adapter.run_render` ([L433-L444](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L433-L444)) - Function
  - `scripts.engine_adapter.run_concat` ([L447-L460](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L447-L460)) - Function
  - `scripts.engine_adapter.main` ([L519-L607](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/engine_adapter.py#L519-L607)) - Function


### Visual Diagramming Engine
Translates internal diff data and component relationships into Mermaid.js syntax, managing scope and label truncation for GitHub UI readability.


**Related Classes/Methods**:

- `scripts.diff_to_mermaid.render_mermaid.build`:424-497
- `scripts.diff_to_mermaid._Scope`:265-309
- `scripts.diff_to_mermaid._truncate`:256-258



**Source Files:**

- [`scripts/diff_to_mermaid.py`](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py)
  - `scripts.diff_to_mermaid._esc` ([L247-L253](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L247-L253)) - Function
  - `scripts.diff_to_mermaid._truncate` ([L256-L258](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L256-L258)) - Function
  - `scripts.diff_to_mermaid._Scope` ([L265-L309](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L265-L309)) - Class
  - `scripts.diff_to_mermaid.render_mermaid.build` ([L424-L497](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L424-L497)) - Function
  - `scripts.diff_to_mermaid.render_mermaid.build.emit_edges` ([L435-L447](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L435-L447)) - Function
  - `scripts.diff_to_mermaid.render_mermaid.build.emit_level` ([L449-L466](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/diff_to_mermaid.py#L449-L466)) - Function


### IDE & Web Integration Handler
Detects user environments and generates specialized URLs to enable direct navigation from GitHub comments to local IDEs or webviews.


**Related Classes/Methods**:

- `scripts.build_cta.main`:181-223
- `scripts.build_cta.detect_editors`:36-48
- `scripts.build_cta.webview_url`:62-94



**Source Files:**

- [`scripts/build_cta.py`](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_cta.py)
  - `scripts.build_cta.detect_editors` ([L36-L48](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_cta.py#L36-L48)) - Function
  - `scripts.build_cta.webview_url` ([L62-L94](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_cta.py#L62-L94)) - Function
  - `scripts.build_cta._join_or` ([L97-L103](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_cta.py#L97-L103)) - Function
  - `scripts.build_cta.build_cta` ([L106-L178](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_cta.py#L106-L178)) - Function
  - `scripts.build_cta.build_cta.link` ([L142-L143](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_cta.py#L142-L143)) - Function
  - `scripts.build_cta.main` ([L181-L223](https://github.com/CodeBoarding/CodeBoarding-action/blob/main/.codeboardingscripts/build_cta.py#L181-L223)) - Function




### [FAQ](https://github.com/CodeBoarding/GeneratedOnBoardings/tree/main?tab=readme-ov-file#faq)