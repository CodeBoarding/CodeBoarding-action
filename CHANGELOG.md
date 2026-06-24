# Changelog

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
