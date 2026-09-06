# Changelog

## [1.14.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.13.0...v1.14.0) (2026-09-06)


### Features

* **engine:** generate deeper architecture-aligned component hierarchies ([3ffc1b5](https://github.com/CodeBoarding/CodeBoarding-action/commit/3ffc1b554b10214990e1b5a0d27f7cdf6da98e9d))


### Bug Fixes

* **action:** rebuild 0.13.x baselines once before incremental analysis resumes ([3ffc1b5](https://github.com/CodeBoarding/CodeBoarding-action/commit/3ffc1b554b10214990e1b5a0d27f7cdf6da98e9d))
* **engine:** ground relationships and assign each method to one component ([3ffc1b5](https://github.com/CodeBoarding/CodeBoarding-action/commit/3ffc1b554b10214990e1b5a0d27f7cdf6da98e9d))
* **engine:** keep incremental structure correct for new and deleted components ([3ffc1b5](https://github.com/CodeBoarding/CodeBoarding-action/commit/3ffc1b554b10214990e1b5a0d27f7cdf6da98e9d))
* **engine:** preserve Java and C# package, type, and constructor structure ([3ffc1b5](https://github.com/CodeBoarding/CodeBoarding-action/commit/3ffc1b554b10214990e1b5a0d27f7cdf6da98e9d))
* **engine:** prevent raw fallback names, checkout-path leakage, duplicate names, and empty components ([3ffc1b5](https://github.com/CodeBoarding/CodeBoarding-action/commit/3ffc1b554b10214990e1b5a0d27f7cdf6da98e9d))


### Performance Improvements

* **engine:** reduce analysis time and model-token usage ([3ffc1b5](https://github.com/CodeBoarding/CodeBoarding-action/commit/3ffc1b554b10214990e1b5a0d27f7cdf6da98e9d))

## [1.13.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.12.3...v1.13.0) (2026-08-31)


### Features

* require an explicit llm input and never fall back to hosted credentials ([#104](https://github.com/CodeBoarding/CodeBoarding-action/issues/104)) ([19ffef4](https://github.com/CodeBoarding/CodeBoarding-action/commit/19ffef4423916a3ada1ac95324c636f9b027eb0a))


### Bug Fixes

* bump CodeBoarding engine to 0.13.11 ([#117](https://github.com/CodeBoarding/CodeBoarding-action/issues/117)) ([0049667](https://github.com/CodeBoarding/CodeBoarding-action/commit/0049667f853d4e6f33728e33b014e394eaa9fd9e))

## [1.12.3](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.12.2...v1.12.3) (2026-08-26)


### Bug Fixes

* **action:** restore Core runtime compatibility ([#101](https://github.com/CodeBoarding/CodeBoarding-action/issues/101)) ([b735334](https://github.com/CodeBoarding/CodeBoarding-action/commit/b735334588180ecfce3ef98c9346fc6c68c15acf))

## [1.12.2](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.12.1...v1.12.2) (2026-08-26)


### Bug Fixes

* **action:** pin CodeBoarding 0.13.10 ([#100](https://github.com/CodeBoarding/CodeBoarding-action/issues/100)) ([af6b531](https://github.com/CodeBoarding/CodeBoarding-action/commit/af6b531a72c2b482f57eb6ea56f38e846d9e64ba))
* align tooling and CI with Python 3.12 ([845aa94](https://github.com/CodeBoarding/CodeBoarding-action/commit/845aa9407fc3dcdd4aa8cdfc7512201e3ca6a06f))
* **review:** say when artifacts cannot be listed ([#98](https://github.com/CodeBoarding/CodeBoarding-action/issues/98)) ([dd11082](https://github.com/CodeBoarding/CodeBoarding-action/commit/dd11082547c58659250a3fbe54a9d4c4c4c9cb17))

## [1.12.1](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.12.0...v1.12.1) (2026-08-19)


### Bug Fixes

* **review:** publish state, not scratch, and a boolean that is one ([#95](https://github.com/CodeBoarding/CodeBoarding-action/issues/95)) ([09757de](https://github.com/CodeBoarding/CodeBoarding-action/commit/09757de79e1413c71cabfab1ddadd89aac8f8f1e))
* **review:** stop labelling a base with the run that computed it ([#94](https://github.com/CodeBoarding/CodeBoarding-action/issues/94)) ([f7630ce](https://github.com/CodeBoarding/CodeBoarding-action/commit/f7630ce818327732111f2088f846985be4989e22))

## [1.12.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.11.1...v1.12.0) (2026-08-19)


### Features

* **review:** keep reusable analyses in artifacts, not the cache ([#90](https://github.com/CodeBoarding/CodeBoarding-action/issues/90)) ([539e286](https://github.com/CodeBoarding/CodeBoarding-action/commit/539e286664c0299ecc9a5ecc9bd9c0fc71db9fbb))


### Bug Fixes

* **review:** keep reviews 14 days and base graphs 30 ([#93](https://github.com/CodeBoarding/CodeBoarding-action/issues/93)) ([6a43d14](https://github.com/CodeBoarding/CodeBoarding-action/commit/6a43d14ea33dd070f4d5876bbaedbab5fe26ce10))
* **review:** let a base outlive the reviews that reference it ([#92](https://github.com/CodeBoarding/CodeBoarding-action/issues/92)) ([8ccac44](https://github.com/CodeBoarding/CodeBoarding-action/commit/8ccac440c276ae9d5f8a730f1ee10565751f1720))

## [1.11.1](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.11.0...v1.11.1) (2026-08-18)


### Bug Fixes

* **review:** stop attempting a cache save a comment run cannot make ([#86](https://github.com/CodeBoarding/CodeBoarding-action/issues/86)) ([6bd6951](https://github.com/CodeBoarding/CodeBoarding-action/commit/6bd6951061dfd06f2162e0c2dd50db5d7c791bd0))

## [1.11.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.10.3...v1.11.0) (2026-08-18)


### Features

* **review:** compare against the merge base and reuse the PR's last analysis ([#81](https://github.com/CodeBoarding/CodeBoarding-action/issues/81)) ([5ea3fba](https://github.com/CodeBoarding/CodeBoarding-action/commit/5ea3fbae53392f2226a12c5c482b9901388a08f2))


### Bug Fixes

* install tqdm, which the pinned engine imports but does not require ([#84](https://github.com/CodeBoarding/CodeBoarding-action/issues/84)) ([d8853c4](https://github.com/CodeBoarding/CodeBoarding-action/commit/d8853c41588117e774316767fb577d062796f837))
* stop discarding the health report ([#82](https://github.com/CodeBoarding/CodeBoarding-action/issues/82)) ([f3e0919](https://github.com/CodeBoarding/CodeBoarding-action/commit/f3e09192385dcef764abf17d2ca4ce43c7028d80))

## [1.10.3](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.10.2...v1.10.3) (2026-08-10)


### Bug Fixes

* use stable PR route for webview links ([#75](https://github.com/CodeBoarding/CodeBoarding-action/issues/75)) ([e2f8a71](https://github.com/CodeBoarding/CodeBoarding-action/commit/e2f8a71dcab199c037637cf556af4c155f1b4dbd))

## [1.10.2](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.10.1...v1.10.2) (2026-08-07)


### Bug Fixes

* **action:** force release-please via manual dispatch ([#77](https://github.com/CodeBoarding/CodeBoarding-action/issues/77)) ([2fe1d47](https://github.com/CodeBoarding/CodeBoarding-action/commit/2fe1d476e6bad4f55db1bd5e6c88c621d6ec5e2a))

## [1.10.1](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.10.0...v1.10.1) (2026-08-05)


### Bug Fixes

* update CodeBoarding dependency to 0.13.6 ([#73](https://github.com/CodeBoarding/CodeBoarding-action/issues/73)) ([291fe31](https://github.com/CodeBoarding/CodeBoarding-action/commit/291fe3185dad84e06aac5532dd1fab93f6e124f8))

## [1.10.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.9.1...v1.10.0) (2026-08-04)


### Features

* add manual release trigger ([#71](https://github.com/CodeBoarding/CodeBoarding-action/issues/71)) ([52b4bc3](https://github.com/CodeBoarding/CodeBoarding-action/commit/52b4bc3f0e029a681165769e4ee6ee150ba01c74))

## [1.9.1](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.9.0...v1.9.1) (2026-08-02)


### Bug Fixes

* don't let a late failure erase a review that already posted ([#66](https://github.com/CodeBoarding/CodeBoarding-action/issues/66)) ([4606c1a](https://github.com/CodeBoarding/CodeBoarding-action/commit/4606c1a06dd5810bb88fcbff28a6572ef903a577))
* refresh GitHub OIDC tokens per request ([#65](https://github.com/CodeBoarding/CodeBoarding-action/issues/65)) ([b90704c](https://github.com/CodeBoarding/CodeBoarding-action/commit/b90704c3089d172ded115601b0664f25b8d17530))

## [1.9.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.8.1...v1.9.0) (2026-07-28)


### Features

* **sync:** pull_request delivery strategy for protected branches ([#60](https://github.com/CodeBoarding/CodeBoarding-action/issues/60)) ([0540a47](https://github.com/CodeBoarding/CodeBoarding-action/commit/0540a47a6e243ece6b8c3de50bbafc749a392ce8))

## [1.8.1](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.8.0...v1.8.1) (2026-07-15)


### Bug Fixes

* render global analysis relations in PR diagrams ([#56](https://github.com/CodeBoarding/CodeBoarding-action/issues/56)) ([0a8328b](https://github.com/CodeBoarding/CodeBoarding-action/commit/0a8328baf2b86e92af9a7ea4efb4dc1cab77855b))
* update CodeBoarding dependency to 0.13.2 ([7358554](https://github.com/CodeBoarding/CodeBoarding-action/commit/735855451ca372a4e4c1d3275d0167e9bedf4385))

## [1.8.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.7.0...v1.8.0) (2026-07-14)


### Features

* migrate to Core's git-free incremental ([#401](https://github.com/CodeBoarding/CodeBoarding-action/issues/401)) ([#52](https://github.com/CodeBoarding/CodeBoarding-action/issues/52)) ([d585139](https://github.com/CodeBoarding/CodeBoarding-action/commit/d5851390a0ab3a99aba064476c69cbc556957ec7))


### Bug Fixes

* update Core dependency to 0.13.1 ([#54](https://github.com/CodeBoarding/CodeBoarding-action/issues/54)) ([e651a9a](https://github.com/CodeBoarding/CodeBoarding-action/commit/e651a9a71ec5b26198ac1a8f3c137f28fd383e76))

## [1.7.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.6.1...v1.7.0) (2026-06-30)


### Features

* scope CI workflow permissions to least privilege ([#50](https://github.com/CodeBoarding/CodeBoarding-action/issues/50)) ([cb64c17](https://github.com/CodeBoarding/CodeBoarding-action/commit/cb64c17291deaff5db3a7c67a4ce2f4b1c7887b2))

## [1.6.1](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.6.0...v1.6.1) (2026-06-24)


### Bug Fixes

* **sync:** commit baseline as codeboarding-review[bot], not codeboarding[bot] ([6f097bb](https://github.com/CodeBoarding/CodeBoarding-action/commit/6f097bb48ecc5ea4239c8ae73cc118fd6e4c73a8))

## [1.6.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.5.4...v1.6.0) (2026-06-23)


### Features

* **cta:** emit short GitHub-style webview PR links ([6f6bb39](https://github.com/CodeBoarding/CodeBoarding-action/commit/6f6bb39e4e3a4fb08d5b3fdf8305e91e522e35ca))

## [1.5.4](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.5.3...v1.5.4) (2026-06-19)


### Bug Fixes

* review inherits committed baseline depth for the PR diff ([#45](https://github.com/CodeBoarding/CodeBoarding-action/issues/45)) ([6aa40f2](https://github.com/CodeBoarding/CodeBoarding-action/commit/6aa40f2349afe7a2659af86f454d8ff2e2f44415))

## [1.5.3](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.5.2...v1.5.3) (2026-06-19)


### Bug Fixes

* compare review diagrams against target baseline ([#39](https://github.com/CodeBoarding/CodeBoarding-action/issues/39)) ([7339fbf](https://github.com/CodeBoarding/CodeBoarding-action/commit/7339fbf500f4fa9d4d31a60a17b692d1a3d8d05f))
* install CodeBoarding engine from PyPI ([#44](https://github.com/CodeBoarding/CodeBoarding-action/issues/44)) ([224aa76](https://github.com/CodeBoarding/CodeBoarding-action/commit/224aa7613e472b07f1849b5fcc360e3cf3f98a5e))

## [1.5.2](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.5.1...v1.5.2) (2026-06-17)


### Bug Fixes

* remove stale engine venv before recreating it ([fc3af60](https://github.com/CodeBoarding/CodeBoarding-action/commit/fc3af6047df9cb05c86f6c97661b9c46892ab199))

## [1.5.1](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.5.0...v1.5.1) (2026-06-17)


### Bug Fixes

* make a CodeBoarding license work with the hosted proxy — license mode now mints the OIDC token and packs the license into the bearer (`<jwt>~codeboarding-license~<license>`), which the proxy splits, verifies, and uses to skip the free quota (previously sent the raw license as the bearer → 401 Invalid GitHub OIDC token) ([#41](https://github.com/CodeBoarding/CodeBoarding-action/issues/41)) ([56ed9e9](https://github.com/CodeBoarding/CodeBoarding-action/commit/56ed9e9cda6d084b194f3e3124dae296971c5cad))
* rebuild the engine venv when its cached interpreter symlink is stale, so a runner Python patch bump no longer breaks runs with "Broken symlink … was the underlying Python interpreter removed?" ([#41](https://github.com/CodeBoarding/CodeBoarding-action/issues/41)) ([56ed9e9](https://github.com/CodeBoarding/CodeBoarding-action/commit/56ed9e9cda6d084b194f3e3124dae296971c5cad))
* set up Java and .NET inside action ([#37](https://github.com/CodeBoarding/CodeBoarding-action/issues/37)) ([a7164ab](https://github.com/CodeBoarding/CodeBoarding-action/commit/a7164abe286eaf7c5f87184e05df175d92a0777f))

## [1.5.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.4.0...v1.5.0) (2026-06-15)


### Features

* auto-detect depth_level from committed baseline ([#34](https://github.com/CodeBoarding/CodeBoarding-action/issues/34)) ([eb58256](https://github.com/CodeBoarding/CodeBoarding-action/commit/eb58256d9fb36beec54a0f9cbe5611276ac1a0b8))

## [1.4.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.3.0...v1.4.0) (2026-06-14)


### Features

* add /codeboarding-feedback command to capture PR feedback via PostHog ([#32](https://github.com/CodeBoarding/CodeBoarding-action/issues/32)) ([2df93e2](https://github.com/CodeBoarding/CodeBoarding-action/commit/2df93e2a769be8f30dcb200682e3be26b16b6e82))
* add sync mode — commit a versioned architecture baseline on push ([#28](https://github.com/CodeBoarding/CodeBoarding-action/issues/28)) ([290e36b](https://github.com/CodeBoarding/CodeBoarding-action/commit/290e36b6f828f10576da1477bcaf07c8f53160ec))
* merge the PR-comment browser + editor CTAs into one line ([#30](https://github.com/CodeBoarding/CodeBoarding-action/issues/30)) ([3168018](https://github.com/CodeBoarding/CodeBoarding-action/commit/31680187d7cd65a2c3346d502814b6d759ca4fa6))


### Bug Fixes

* drop [skip ci] from bot commits so merges trigger sync; guard the loop without it ([#31](https://github.com/CodeBoarding/CodeBoarding-action/issues/31)) ([cf66577](https://github.com/CodeBoarding/CodeBoarding-action/commit/cf66577cf0658a2d91e0d395ad14b2fa5ee5a980))

## [1.3.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.2.0...v1.3.0) (2026-06-13)


### Features

* link the PR comment to the hosted webview (webview_base_url, default ([c8f0dcb](https://github.com/CodeBoarding/CodeBoarding-action/commit/c8f0dcbb5b2314b1ac3f1524a96fea98a02c2994))
* list each changed component's touched files in the PR comment ([#27](https://github.com/CodeBoarding/CodeBoarding-action/issues/27)) ([c8f0dcb](https://github.com/CodeBoarding/CodeBoarding-action/commit/c8f0dcbb5b2314b1ac3f1524a96fea98a02c2994))


### Bug Fixes

* **ci:** match the action's full key normalization + add preflight ([e9aa643](https://github.com/CodeBoarding/CodeBoarding-action/commit/e9aa6435ce9f0ffe6726d5c1a3758980f79f1c3b))
* **ci:** strip OpenRouter key + read model pins from secrets in baseline refresh ([8e88d8c](https://github.com/CodeBoarding/CodeBoarding-action/commit/8e88d8ccecb4a199439845c1dd14e40ed46eb52c))

## [1.2.0](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.1.0...v1.2.0) (2026-06-10)


### Features

* /codeboarding posts a new comment per run instead of updating in place ([74538af](https://github.com/CodeBoarding/CodeBoarding-action/commit/74538afa7580665b731d9afae968d8324aa64d20))
* /codeboarding posts a new comment per run instead of updating in place ([b21f1ff](https://github.com/CodeBoarding/CodeBoarding-action/commit/b21f1ffaed6b205f222ea5036aa89bd0d6d02f50))
* seed the base static-analysis pkl so PR head analysis runs incrementally ([#18](https://github.com/CodeBoarding/CodeBoarding-action/issues/18)) ([2164398](https://github.com/CodeBoarding/CodeBoarding-action/commit/216439844bfbf3994fc1e2a188a66a2cf5b96f48))
