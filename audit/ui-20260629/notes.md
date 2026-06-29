# Douyin Slice Optimizer UI Audit

Date: 2026-06-29

Scope: local dashboard at `http://127.0.0.1:8000/` and Vite dev dashboard at `http://127.0.0.1:5173/static/dashboard/`.

Data state during audit: 1 video, 30 candidate segments, 1 export, 0 training samples.

Primary scope update: PC desktop is the current priority. Mobile findings are retained for later responsive work, but should not drive the near-term design backlog.

Viewports:
- Desktop: 1280 x 720
- Mobile: 390 x 844

## PC Priority Summary

1. High - Top-nav view changes preserve scroll position.
   Evidence: `05-feedback.png`; switching from a scrolled simulation page opened feedback in the middle. On PC this feels like the page has loaded into the wrong section and weakens trust in the navigation model.

2. High - Simulation page hides its primary value below a large quality sentinel.
   Evidence: `04-simulation.png` and `04b-simulation-cards.png`. The simulation tab should immediately answer "which candidates are likely to expand and why"; currently the first viewport is mostly quality diagnostics.

3. Medium - Candidate review card density is high.
   Evidence: `03-candidates-review-viewport.png`. The screen is functional and information-rich, but each row asks the reviewer to parse title, transcript, structure, tags, score, manual status, quality status, export status, cover time, and actions at once.

4. Medium - Workbench quality sentinel is useful but visually dominant.
   Evidence: `08-fastapi-root.png`. The gate is valuable, but the lower detail rows create a heavy first-screen mass. Consider a denser summary-first presentation on PC.

5. Medium - Vite dev entry gives a false empty state.
   Evidence: `01-vite-initial-empty.png`; FastAPI root is correct in `08-fastapi-root.png`. This is mostly a development/testing experience issue, but it can mislead anyone reviewing the UI through Vite.

6. Low - Feedback empty states are clear but too large.
   Evidence: `05b-feedback-top.png`. On PC the two-column layout works, but empty analytic panels consume more attention than the actionable import/account setup controls.

## Accepted Screenshots

1. `01-vite-initial-empty.png` - Vite dev initial state before manual refresh.
2. `02-workbench-data-default-desktop.png` - Vite dev workbench after refresh.
3. `03-candidates-review-viewport.png` - Candidate review with preview/detail inspector.
4. `04-simulation.png` - Simulation page first viewport.
5. `04b-simulation-cards.png` - Simulation cards after scrolling past quality sentinel.
6. `05-feedback.png` - Feedback page after switching from a scrolled simulation state.
7. `05b-feedback-top.png` - Feedback page after scrolling to top.
8. `06-mobile-workbench.png` - Mobile workbench.
9. `07-mobile-candidates.png` - Mobile candidate review.
10. `08-fastapi-root.png` - FastAPI root entry with injected initial state.

## Step Health

1. FastAPI root workbench - healthy. The real entry loads the selected video and correct counts immediately.
2. Vite dev initial state - needs attention. It shows zeros and sends the user to "import first" even though the API has data.
3. Desktop workbench - mostly healthy. Workflow guidance and quality gate are useful, but the quality panel is dense.
4. Candidate review - strong core screen. List, risk status, export state, and video preview work together, but card density is high.
5. Simulation first viewport - needs attention. The quality sentinel dominates the page before the user sees simulation results.
6. Simulation cards - healthy once reached. Stage bars, bottlenecks, audience clusters, and actions are decision-oriented.
7. Feedback after tab switch - needs attention. The page preserves prior scroll position and can open in the middle of the view.
8. Feedback top - mixed. Import and account state are understandable, but empty-state blocks take a lot of space.
9. Mobile workbench - healthy. The process guide becomes a clear vertical flow with no page overflow.
10. Mobile candidate review - unhealthy. The page horizontally overflows to 797px, and preview/detail starts after roughly 5079px.

## Strengths

- The product feels like an operational tool, not a landing page. The persistent top nav, workflow rail, data tables, candidate queue, and inspector match the job.
- The workflow guide correctly translates backend state into a next action. In the current data state it guides the user toward feedback import.
- Candidate review is the strongest interaction pattern: score, status, quality flags, export state, and video preview are all present in one workflow.
- Quality Gate language is concrete. "复核 / 先处理 ASR / 高风险" is much better than a vague warning banner.
- Accessibility foundations are present: skip link, visible focus style, `aria-live` for guide/toast areas, labeled inputs/selects, reduced-motion CSS, and keyboard-selectable candidate cards.

## UX Risks

1. High - Mobile candidate review breaks horizontally.
   Evidence: `07-mobile-candidates.png`; measured document width was 797px on a 390px viewport. Source likely starts at `frontend/src/styles.css:1141`, where `.review-brief` keeps a desktop multi-column grid and has no mobile override near the `840px` breakpoint.

2. High - Candidate details are too far away on mobile.
   Evidence: `07-mobile-candidates.png`; the inspector began around y=5079 after the full candidate list. A mobile reviewer needs preview/export/details close to the selected card.

3. High - Top-nav view changes preserve scroll position.
   Evidence: `05-feedback.png`; switching from a scrolled simulation page opened feedback in the middle. `setView()` changes state but does not reset or target scroll at `frontend/src/composables/useDashboard.ts:307`.

4. Medium - Simulation page hides its primary value below a large quality sentinel.
   Evidence: `04-simulation.png` and `04b-simulation-cards.png`. `SimulationView.vue` renders `<QualitySentinel compact />` before summary/cards at `frontend/src/components/SimulationView.vue:18`.

5. Medium - Vite dev entry gives a false empty state.
   Evidence: `01-vite-initial-empty.png`; FastAPI root is correct in `08-fastapi-root.png`. `App.vue:33` loads runtime/feedback and only loads quality/suggestions if `selectedVideoId` already exists, but it does not refresh videos/stats when no initial state is injected.

6. Medium - Candidate cards carry too many simultaneous dimensions.
   Evidence: `03-candidates-review-viewport.png`. Time, title, transcript, structure, tags, score, review state, quality state, export state, cover, copy actions, preview, and export all compete at once.

7. Medium - Feedback page empty states are clear but too dominant.
   Evidence: `05b-feedback-top.png`. The import path is present, but several large empty blocks make "what should I do next?" less immediate.

## Accessibility Risks

- Mobile horizontal overflow is also an accessibility issue: zoomed and touch users will lose content off-screen.
- Icon-only buttons use `title` in several places (`CandidateWorkbench.vue:84`, `ProgramWorkbench.vue:77`). Add explicit `aria-label` and keep touch targets closer to 44px on mobile.
- Candidate cards use `aria-label="候选片段 N"`, which is keyboard-friendly but not descriptive enough. Include title, score, and status in the accessible name or description.
- The app uses color-coded states, but most badges also include text. Keep that pattern and avoid relying on color alone in bars/meters.
- This audit did not run a screen reader, automated contrast checker, or keyboard-only full traversal, so it should not be treated as WCAG compliance.

## Recommendations

1. Fix mobile candidate layout first.
   Make `.review-brief` a single-column or two-column compact summary under `840px`, and put primary actions below the summary. Add an immediate "查看详情 / 导出" action near the selected mobile card.

2. Reset scroll on top-level tab changes.
   In `setView()`, scroll to the relevant panel top after changing views. Keep targeted scroll behavior for guide actions, but top nav should feel like page navigation.

3. Create a truly compact quality sentinel variant.
   On simulation, show the four simulation stats and first card before the full quality detail. Keep gate status as a small banner with an expandable detail section.

4. Fix Vite dev bootstrapping.
   If `initial.videos` is empty, call `refreshVideos()` or at least `loadStats()` plus `/videos` on mount. This prevents a false "暂无节目 / 先导入" state during development.

5. Reduce candidate-card scanning load.
   Make the card hierarchy: rank/time/title, score, decision, primary action. Move transcript, structure, and secondary metadata into inspector or an expandable row.

6. Tighten feedback information architecture.
   Put "导入指标", "抖音账号状态", and "当前数据口径" before low-value empty analytics blocks when there are no samples.

7. Add explicit labels and mobile target sizing.
   Use `aria-label` for icon-only buttons, and make the mini action buttons larger on touch breakpoints.

## Evidence Limits

- Screenshots and DOM measurements were captured from the current local app only.
- No user interview, task timing, or analytics data was available.
- No destructive actions were performed; export/review/login flows were not submitted.
- The FastAPI root and Vite dev entry differ in bootstrapping behavior, so findings call out which surface produced the evidence.
