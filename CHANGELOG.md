# Changelog

## [1.5.1](https://github.com/CodeBoarding/CodeBoarding-action/compare/v1.5.0...v1.5.1) (2026-06-17)


### Bug Fixes

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
