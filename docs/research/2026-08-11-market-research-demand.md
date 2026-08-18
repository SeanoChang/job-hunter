---
title: Market research — who needs job-hunter, and what would they pay for
date: 2026-08-11
type: report
status: current
---

# Market research: demand for a BYO-agent job-hunting toolkit

Method: four parallel research agents (job-seeker pain, competitor landscape,
BYO-agent demand signals, market sizing / willingness to pay), each followed by
a citation-verification agent that fetched every cited URL and spot-checked the
numbers. 54 references total; verifier corrections are applied in the text and
flagged inline where a source's claim needed softening. Genre note: written as a
report (bringing the reader up to date on external evidence), following the
existing `docs/research/` memo conventions.

> [!TLDR] Demand is validated — and the category is no longer empty
>
> The exact product shape ("job search that runs inside your own coding agent,
> locally, in files you own") produced two of the fastest-growing open-source
> repos of 2026: career-ops (63.5k stars) and ai-job-search (31.1k stars). That
> is both the strongest possible demand proof and a real competitor. The
> defensible whitespace is the layer none of them have: a queryable temporal
> data layer — SQLite posting history, field-level diffs, repost lineage,
> ghost-job scoring — exposed over MCP. The nearest architectural cousin to that
> exact position (jobops: local SQLite + MCP) has 1 GitHub star.

## What this changes for job-hunter (synthesis)

1. **Differentiate on the temporal database, not the fetcher.** Fetching
   Greenhouse/Lever/Ashby is commoditized — career-ops, HiringCafe,
   whenthisjobwasposted.com, and JobSpy all do it. Nobody persists longitudinal,
   user-queryable history with diffs and closure signals. That is the core; say
   so in the README's first paragraph.
2. **Ghost-job scoring is the headline feature.** 20–33% of postings never
   produce a hire (JOLTS-based analysis: 30% in June 2025; Greenhouse's own
   platform estimate: 18–22% per quarter), LinkedIn and Greenhouse shipped
   verification badges in 2025, and the FTC formed a task force naming deceptive
   job ads. Every existing "ghost job detector" is a stateless point-in-time
   checker; ours would be computed from owned history.
3. **Position explicitly against the "AI doom loop."** Greenhouse's CEO is on
   record (Fortune, July 2026) attacking $20 mass-apply tools; applications per
   posting hit 254 (from ~115 in 2022). A curated company panel with deep
   per-application workspaces is the credible counter-thesis — "assist, don't
   spray" belongs in the positioning verbatim.
4. **Ship the MCP server early and list it in the official registry.** ~9,650
   registry records and first-party MCP support in every major client, yet no
   incumbent (Teal, Huntr, Simplify, Jobscan, HiringCafe) ships an agent-native
   interface as of August 2026. The window to own "the MCP server for job
   search" is open and first-mover mindshare on GitHub is sticky.
5. **Consider interop with career-ops rather than head-on replacement.** It
   already covers our fetch targets with markdown/YAML storage; an importer that
   upgrades its flat files into our temporal store converts its 12.5k forks into
   a funnel instead of a moat we have to climb.
6. **Treat individual monetization as near-zero by design.** The audience that
   wants local-first SQLite + MCP self-hosts free tools; their subscription
   money already goes to the coding agent. The billable surfaces, if ever, are
   hosted historical ATS data (a fresh install can't backfill history) and
   multi-candidate seats for coaches/outplacement — the HashiCorp open-core
   pattern. Plan for a $1–10M ARR ceiling band, not the theoretical
   $150–450M
   SAM.
7. **Distribution playbook is proven: Reddit + GitHub + a personal funnel
   story.** HiringCafe went 0→1.3M MAU on $0 marketing via Reddit; ai-job-search
   rode a documented 69 applications → 20 interviews → 1 offer story to #1 on
   GitHub trending. A bare Show HN without a hook got 1 point. Launch with our
   own tracked-panel ghost-job statistics as the hook.

## 1. Quantified job-seeker pain

The pain is measured, recent, and worst for exactly our target user.

- **Volume inflation.** Applications per posting on Greenhouse averaged 254
  across ~175,000 live positions by mid-2026, up from ~115 in 2022; applications
  per recruiter up 412%. LinkedIn received ~11,000 applications per minute in
  mid-2025 (+45% YoY). In Greenhouse's 2025 survey (4,136 respondents), 34% of
  recruiters spend up to half their week filtering spam applications.
- **Response collapse.** ~75% of applications receive zero response (aggregated
  index, directional); 61% of candidates were ghosted _after_ an interview
  (Greenhouse 2024, up 9 points from April 2024); 8 in 10 hiring managers admit
  to ghosting (Resume Genius, 625 hiring managers, 2024). Technology is the
  worst-responding industry measured, at ~5% response rates versus healthcare's
  20%.
- **Ghost jobs are structural, not anecdotal.** JOLTS-based analysis: 30% of
  June 2025 openings (2.2M+ roles) never produced a hire, with the Information
  sector at 44–48%; the gap has run 28–38% monthly since 2021 versus under 10%
  pre-pandemic. Roughly 1 in 5 employers admit deliberately maintaining unfilled
  postings (Clarify Capital, 1,000 employers); ~40% of hiring managers
  acknowledged posting ghost jobs in the past year (Resume Builder, 649 hiring
  managers). Platform response: LinkedIn now tags more than half its listings
  "verified," Greenhouse launched a verified-job badge, and the FTC's February
  2025 Joint Labor Task Force names deceptive job advertising as a priority.
- **The trust collapse is mutual.** Only 8% of job seekers call AI-driven hiring
  fair, while 70% of hiring managers trust AI to decide faster/better; 41% of
  seekers admit using prompt injections against AI filters (Greenhouse 2025).
- **Tech workers specifically.** 74% of developers report struggling to land
  jobs (HackerRank, 13,732 respondents); named causes are ghost jobs, AI-flooded
  pipelines, and weeks-long response times. Junior hiring grew 7–9% YoY versus
  19–22% for senior roles.
- **Tracking demand is proven but shallow.** Huntr claims 500k+ users, Simplify
  claims 1.5–2M (both vendor-reported); all incumbent trackers stop at "applied"
  — the 61% post-interview ghosting phase is exactly where they end and our
  workspace begins.

## 2. Competitor landscape

Every traction-holding tool is hosted SaaS monetized by AI upsells or employer
revenue. The three questions the design bets on all came back "no one":

- **Local-first data the user owns?** No product with traction. Teal (650k+
  members, $29/mo upsell), Huntr (Pro $40/mo, 100-job free cap), Simplify (free
  tracker, about $40/mo AI tier, employer-paid revenue), Careerflow — all cloud.
  The only local-first entrants are hobby-stage (jobops: SQLite + MCP full-loop
  server, 1 star).
- **BYO-agent / MCP integration?** Only a 2025–26 crop of thin MCP wrappers
  around job APIs, none with traction. Incumbents sell _their_ hosted AI at
  $29–50/mo — the opposite strategy to using the agent you already pay for.
- **Temporal posting history with ghost scoring?** Nobody.
  whenthisjobwasposted.com covers 37+ ATS platforms but keeps no server-side
  history (Wayback lookups + browser localStorage); browser-extension detectors
  score single postings from surface signals; HiringCafe filters stale jobs
  internally but exposes no history or diffs to users.

Adjacent lessons: HiringCafe's 0→1.3M MAU on
$0 marketing proves demand for
ATS-direct, no-promoted-jobs sourcing. Otta (1.7M users) being acquired by an
employer-branding firm illustrates that every hosted "candidate-first" product
eventually monetizes employers. The auto-applier category (LazyApply
$99–999/yr,
Sonara, AIHawk) has the worst reputation and legal fragility — AIHawk's
third-party platform plugins were removed over copyright and the maintainer
pivoted to detection-evasion tooling, which validates our direct-public-ATS-API
approach over scraping.

## 3. BYO-agent demand signals

- **The category exploded in 2026.** career-ops: 63.5k stars / 12.5k forks; runs
  in Claude Code/Codex/Copilot via skills; scans 100+ companies across
  Ashby/Greenhouse/Lever/Wellfound; A–F scoring; local markdown/YAML storage; Go
  TUI. ai-job-search: 31.1k stars / 10.7k forks since March 2026, built on
  Claude Code, propelled by the author's documented layoff-to-hired funnel.
  Fork-to-star ratios near 1:3 to 1:5 indicate people run these, not just
  bookmark them. Neither has SQLite, field-level diffs, repost detection, or
  ghost scoring.
- **People already pay for this exact workflow as Claude Code skills.** Aakash
  Gupta's "Claude Code Job Search OS" sells 18 skills at $49 one-time with
  75-seat cohorts (March 2026) — paid demand for a weaker version of what we'd
  open-source.
- **MCP is real distribution.** ~2,000 official-registry entries at the November
  2025 anniversary (+407% since September); ~10,000 active public servers and
  97M+ monthly SDK downloads by December 2025; 9,652 registry records and 15,926
  `mcp-server`-tagged repos by May 2026; first-party adoption by OpenAI,
  Microsoft, Google, AWS, GitHub. One listing reaches every major client.
- **Skills kits scale as a product form.** obra/superpowers: 270.3k stars for a
  folder of skills + methodology. The local-first plain-text audience is seven
  figures (Obsidian ~1–1.5M users, third-party estimate).
- **Caveat: traction is winner-skewed.** A local-first Claude Code job tracker
  on Show HN got 1 point; the winners had a personal story or trending momentum.
  In-audience skeptics argue blind ATS applications lose to networking
  regardless of tooling — our post-apply research workspace aligns with that
  critique rather than fighting it.

## 4. Market size and willingness to pay

- **Search volume.** US market is "low-hire, low-fire": 7.4M openings, ~5.3M
  hires/month, ~3.2M quits/month (June 2026 JOLTS). Tech layoffs tracked by
  layoffs.fyi: ~265k (2023) → ~152k (2024) → ~123k (2025), ~540k over three
  years. Software engineers average ~20 weeks to land a role; a 4–6 month search
  is the modal tech experience, so the tool is used intensively for a bounded,
  high-stakes window.
- **Candidates demonstrably pay.** LinkedIn Premium passed
  $2B trailing-12-mo
  subscription revenue (January 2025, from $1.7B in March
  2024), with ~40% of Premium subscribers using its AI job features. Teal+ is
  about $29/mo, Jobscan about $49.95/mo, Simplify+ about
  $39.99/mo. Career
  coaching is roughly a $16.5B global market (2025, broad
  definition — verifier corrected this from "US").
- **The BYO-agent installed base is mainstream.** ChatGPT ~50M paying
  subscribers (February 2026); Claude paid subscriptions more than doubled in
  2026 (US card-panel data); Claude Code went
  $1B annualized (November 2025)
  → $2.5B (February 2026), with 71% of AI-agent
  users calling it their primary tool in a 15k-developer survey. Several million
  paying coding-agent subscribers exist for whom job-hunter's marginal LLM cost
  is $0.
- **Honest SAM math.** ~1–1.5M US / ~4–6M global developer job searches per
  year; intersected with paying-agent users, ~1–2M reachable search episodes per
  year; at demonstrated price points of
  $100–300 per search episode, the
  theoretical serviceable market is about $150–450M/yr
  — but realistic open-source capture (1–3% of users at
  $50–150/yr on hosted-data or coach-seat
  tiers) is a $1–10M ARR band. The
  strongest for-pay argument is stakes: a median US engineer forgoes $10–12k
  gross pay per month of search, so shortening a 20-week search by a week is
  worth hundreds of dollars. The strongest against: adverse selection — the
  local-first CLI audience is precisely the audience that self-hosts free tools
  and churns the day they sign an offer.

## Verifier corrections applied

- Interview Guys "Ghost Jobs Exposed" published November 12, 2025 (not 2026).
- Resume Genius ghost-jobs explainer published March 2025, updated October 2025
  (not 2024); its stats (1.6M stale LinkedIn postings, 32% frustration)
  confirmed.
- KPMG JOLTS article's real title is "Job openings rise, layoffs fall" (March
  13, 2026).
- A claimed 24.5-week average unemployment duration was not on its cited page
  (which cites 26 weeks, BLS, after layoff) — dropped; only the ~20-week SWE
  figure is kept.
- Simplify+ weekly/quarterly promo prices were not on the cited review —
  dropped; $39.99/mo confirmed.
- The career-coaching $16.5B figure is global, not US.
- Claude Code "17.7M→29M installs" is a 30-day moving average of daily installs,
  not cumulative — cited here only as directional growth.
- The eWeek, ToolDirectory, FinSMEs, and Market Research Intellect pages blocked
  direct fetch (403) and were confirmed via search index — treated as confirmed
  but noted.

## References

### Job-seeker pain

- [Greenhouse 2025 AI in Hiring survey](https://www.greenhouse.com/newsroom/an-ai-trust-crisis-70-of-hiring-managers-trust-ai-to-make-faster-and-better-hiring-decisions-only-8-of-job-seekers-call-it-fair)
  — Greenhouse, November 2025. 4,136 respondents; 69% hit fake postings, 41%
  prompt-inject, 8% call AI hiring fair.
- [Greenhouse CEO on the "AI doom loop"](https://fortune.com/2026/07/27/greenhouse-ceo-daniel-chait-ai-doom-loop-job-seekers-spam-interview-applications-unemployment/)
  — Fortune, July 2026. 254 apps/posting, +412% per recruiter, $20 mass-apply
  tools.
- [MyPerfectResume "Ghost Job Economy"](https://www.prweb.com/releases/myperfectresume-reveals-the-ghost-job-economy-1-in-3-us-job-listings-lead-nowhere-302609641.html)
  — PRWeb, November 2025. JOLTS-based: 30% of June 2025 openings never produced
  a hire; Information sector 44–48%.
- [Clarify Capital, Ghost Jobs 2.0](https://clarifycapital.com/ghost-jobs)
  — 2025. 1,000 employers; 1 in 8 postings open 4+ months; ~1 in 5 deliberately
  unfilled.
- [Stack Overflow blog on ghost jobs](https://stackoverflow.blog/2024/12/26/the-ghost-jobs-haunting-your-career-search/)
  — December 2024. Resume Builder survey of 649 hiring managers: ~40% posted
  ghost jobs.
- [Ghosting Index](https://blog.theinterviewguys.com/the-2025-ghosting-index/) —
  The Interview Guys, September 2025. Aggregation: ~75% zero response, 61%
  post-interview ghosting, tech ~5% response rate. Directional.
- [Ghost Jobs Exposed](https://blog.theinterviewguys.com/ghost-jobs-exposed/) —
  The Interview Guys, November 2025. LinkedIn verified-badge coverage,
  Greenhouse 18–22% estimate, February 2025 FTC task force.
- [HackerRank](https://www.hackerrank.com/reports/developer-skills-report-2025)
  — 2025 Developer Skills Report. 13,732 respondents; 74% of developers struggle
  to land jobs.
- [Ghost-jobs explainer](https://resumegenius.com/blog/job-hunting/ghost-jobs) —
  Resume Genius, March 2025. ~1.6M US LinkedIn postings older than 30 days.
- [Ghosting survey](https://resumegenius.com/blog/job-hunting/job-ghosting) —
  Resume Genius, May 2024. 625 hiring managers; 80% admit ghosting.
- [Application flood](https://www.eweek.com/news/ai-job-applications-linkedin/)
  — eWeek reporting LinkedIn data via NYT, 2025. ~11,000 applications per
  minute, +45% YoY.
- [Huntr job tracker](https://huntr.co/product/job-tracker) — vendor-reported
  500k+ users.
- [Simplify overview](https://tooldirectory.ai/tools/simplify) —
  ToolDirectory, 2026. 1.5M+ seekers, 200M+ applications (vendor-reported).

### Competitor landscape

- [HiringCafe](https://blog.hiring.cafe/p/scaling-hiringcafe-from-0-to-1m-users)
  — "Scaling HiringCafe from 0 to 1M+ users," Ali Mir, September 2025. 1.3M+
  MAU, $0 marketing, Reddit-driven.
- [AIHawk Jobs_Applier_AI_Agent](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk)
  — GitHub. 30.1k stars; plugins removed over copyright.
- [Simplify](https://simplify.jobs/) — 2M+ seekers claim, employer-paid model.
- [HR Dive on the phantom gap](https://www.hrdive.com/news/us-job-listings-go-nowhere-creating-a-ghost-job-economy/805448/)
  — November 2025. 7.4M openings vs 5.2M hires.
- [Huntr pricing](https://huntr.co/pricing) — $40/mo Pro, 100-job free cap.
- [Otta acquired by Welcome to the Jungle](https://press.welcometothejungle.com/en/news/uk-recruitment-platform-otta-acquired-by-welcome-to-the-jungle)
  — January 2024. 1.7M users.
- [Teal review](https://resumehog.com/blog/posts/teal-hq-review-2026-is-this-job-search-tool-worth-it.html)
  — ResumeHog, March 2026. 650k+ members; pricing and criticisms.
- [LazyApply review](https://applyghost.com/blog/lazyapply-review) — ApplyGhost,
  March 2026. $99–999/yr; documented misfills.
- [jobops MCP server](https://github.com/HireBridge/jobops) — nearest
  architectural cousin: local SQLite + MCP, 1 star, no temporal features.
- [whenthisjobwasposted.com](https://whenthisjobwasposted.com/about) — 37+ ATS
  platforms, explicitly no server-side temporal database.
- [Simplify funding](https://tracxn.com/d/companies/simplify-jobs/__Nghq6k46Vs-N_rZ2M26VOUDcy5eji4eK0ZC_K36a0HQ/funding-and-investors)
  — Tracxn. $4.35M seed total, no Series A.
- [Rezi](https://www.rezi.ai/) — 4.3M-user claim; $29/mo Pro.
- [Levels.fyi employer offerings](https://www.levels.fyi/offerings/branding/) —
  1.5M unique monthly users; employer-paid board.
- [Final Round AI seed round](https://www.finsmes.com/2025/01/final-round-ai-raises-6-88m-in-seed-funding.html)
  — FinSMEs, January 2025. $6.88M for an interview copilot.

### BYO-agent demand

- [ai-job-search](https://github.com/MadsLorentzen/ai-job-search) —
  GitHub, 2026. 31.1k stars; Claude Code job-search framework; 69→20→1 funnel.
- [career-ops](https://github.com/santifer/career-ops) — GitHub, 2026. 63.5k
  stars; Ashby/Greenhouse/Lever/Wellfound scanning, TUI, markdown storage.
- [JobSpy](https://github.com/speedyapply/JobSpy) — 4.1k stars; scraping-only
  baseline.
- [One Year of MCP](https://blog.modelcontextprotocol.io/posts/2025-11-25-first-mcp-anniversary/)
  — MCP blog, November 2025. Registry growth, first-party adoption.
- [MCP adoption statistics 2026](https://www.digitalapplied.com/blog/mcp-adoption-statistics-2026-model-context-protocol)
  — Digital Applied. 10k+ servers, 97M monthly SDK downloads, Stacklok 41%
  production adoption.
- [obra/superpowers](https://github.com/obra/superpowers) — 270.3k stars;
  skills-kit precedent.
- [Claude Code Job Search OS](https://www.news.aakashg.com/p/job-search-os) —
  Aakash Gupta, March 2026. 18 skills at $49; paid demand for the workflow.
- [Greenhouse 2024 State of Job Hunting](https://www.greenhouse.com/blog/greenhouse-2024-state-of-job-hunting-report)
  — December 2024. 18–22% ghost jobs per quarter; 61% post-interview ghosting.
- [Obsidian statistics](https://fueler.io/blog/obsidian-usage-revenue-valuation-growth-statistics)
  — Fueler, 2025. 1–1.5M user estimate (unofficial).
- [Hiring Cafe on Hacker News](https://news.ycombinator.com/item?id=42803304) —
  January 2025. Organic interest + networking skepticism.
- [Show HN: local-first tracker](https://news.ycombinator.com/item?id=46891982)
  — early 2026. 1 point — the low end of the category.

### Market sizing

- [JOLTS report](https://www.hiringlab.org/2026/08/04/june-2026-jolts-report/) —
  Indeed Hiring Lab, August 2026 (June data). 7.4M openings; low-hire, low-fire.
- [KPMG](https://kpmg.com/us/en/articles/2026/january-2026-jolts-national.html)
  — on January 2026 JOLTS, March 2026 (real title: "Job openings rise, layoffs
  fall").
- [2025 tech layoffs](https://www.salesforceben.com/how-bad-were-tech-layoffs-in-2025-and-what-can-we-expect-next-year/)
  — Salesforce Ben citing layoffs.fyi, December 2025.
- [2025 layoffs list](https://techcrunch.com/2025/12/22/tech-layoffs-2025-list/)
  — TechCrunch, December 2025; corroborates 2024 totals.
- [Average time to find a job](https://boterview.com/a/average-time-find-job) —
  Boterview, 2026. SWE ~20 weeks.
- [LinkedIn Premium passes $2B](https://finance.yahoo.com/news/linkedin-passes-2b-premium-revenues-230224866.html)
  — TechCrunch via Yahoo Finance, January 2025.
- [Pricing](https://www.jobshinobi.com/compare/jobscan-resume-scanner-vs-teal) —
  Jobscan vs Teal, JobShinobi, 2026. Teal+ about $29/mo; Jobscan $49.95/mo.
- [Simplify Copilot review](https://resumehog.com/blog/posts/simplify-copilot-review-2026-is-the-free-autofill-tool-worth-it.html)
  — ResumeHog, 2026. $39.99/mo Simplify+.
- [Career coaching market](https://www.marketresearchintellect.com/product/career-coaching-service-market/)
  — Market Research Intellect, 2025. About $16.5B global.
- [Market](https://www.wiseguyreports.com/reports/job-search-software-market) —
  job-search software market, WiseGuyReports. About $4.7B (2025) → $10B (2035).
- [ChatGPT statistics](https://technologychecker.io/blog/chatgpt-statistics) —
  TechnologyChecker, August 2026. ~50M paying subscribers.
- [Claude monthly users](https://www.statista.com/statistics/1659723/global-monthly-claude-users/)
  — Statista. ~245M MAU June 2026.
- [Claude paid subscriptions](https://techcrunch.com/2026/03/28/anthropics-claude-popularity-with-paying-consumers-is-skyrocketing/)
  — TechCrunch, March 2026. More than doubled in 2026 (US card-panel data).
- [Claude Code usage](https://serpsculpt.com/claude-code-usage-statistics/) —
  SerpSculpt, May 2026. $1B → $2.5B annualized.
- [Open source at HashiCorp](https://www.hashicorp.com/en/about/open-source) —
  open-core monetization precedent.
