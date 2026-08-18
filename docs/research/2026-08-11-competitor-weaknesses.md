---
title: Competitor weaknesses — what users actually complain about
date: 2026-08-11
type: report
status: current
---

# Competitor weaknesses: GitHub issues, Reddit, Trustpilot, HN

Method: four research agents, one per competitor cluster, digging primary
complaint surfaces — GitHub issues and discussions (fetched directly), Reddit
threads (Reddit blocks bot fetches; one agent verified threads via the Arctic
Shift archive, others relied on search snippets and dated roundups — flagged
where secondhand), Trustpilot (bot-blocked; figures via search snippets), Chrome
Web Store, Hacker News. Three clusters ran through the research-then-verify
workflow (every cited URL fetched and checked); the search-engines cluster
re-ran as a standalone agent after repeated stalls on Reddit fetches, and
self-reports its confidence caveats. A complaint counts as a pattern only with
2+ independent sources.

> [!TLDR] Competitors' users are asking for exactly what we're building
>
> The maintainer of ai-job-search (31.1k stars), asked about ghost jobs,
> answered that repost cycles were "my strongest suspicion trigger... but never
> certainty" — he could not compute the signal our temporal store is designed to
> compute. career-ops' top community complaint (token-burning scans) and its
> publicly failing flat-file storage are both solved by zero-token ATS-API
> fetching into SQLite. The loudest hosted-tracker complaint — subscription
> billing abuse while unemployed — cannot exist for a local-first free tool. One
> Jobscan review even names the alternative: "Use Claude for free instead."

## Cross-cluster synthesis

**Validated design bets** (complaints our architecture already answers):

1. **Schema-backed storage.** Flat files are failing in public: career-ops shows
   the wrong company's report when two row counters drift (#1623) and spends
   "70%+ filesystem syscalls" reading reports (#2385); ai-job-search's status
   enum is spelled differently across five markdown files, so recorded
   applications silently vanish from reports (#298), and its CSV tracker has
   "six readers but effectively one writer" (#269). SQLite with one schema kills
   these bug classes. Positioning line: the tracker cannot lose your
   application.
2. **Repost detection and ghost scoring.** ai-job-search discussion #140 (the
   maintainer's "never certainty" quote); career-ops lists anti-scam posting
   detection on its roadmap, unbuilt (#1226), and its shared ghosting-data RFC
   (#1506) has gone nowhere; HN ghost-job threads run 209–446 points; New York
   is moving to outlaw ghost postings. Demand is documented in the competitors'
   own trackers.
3. **Zero-token fetching.** career-ops' #1 complaint is cost: a scan sequence
   burned ~500k tokens and locked a Claude Pro user out for ~4 hours (#1089);
   "That cost me $20 USD in tokens" (#410); issue #98 reports 18% of a 5-hour
   budget in 11 minutes. Cause: scanning pulls full board DOM into context.
   Deterministic Greenhouse/Lever/Ashby API fetching costs zero tokens.
4. **Local-first data ownership.** Sonara shut down abruptly on February 1, 2024
   and locked users out of their application history mid-search. Careerflow
   won't let users delete accounts self-serve (two independent reviews); Teal
   exports PDF-only. Billing abuse is the loudest hosted-tracker theme: charges
   after cancellation (Teal, Jobscan), trial charges within 24 hours, renewals
   without notice while jobless.
5. **No promoted inventory, human-in-the-loop.** LinkedIn candidates report real
   postings buried under promoted spam; a 969-point HN thread ("Please don't
   spam people looking for employment") shows the backlash against auto-apply.
   The volume strategy also doesn't work: Wired's LazyApply test got a 0.4%
   interview rate from 5,000 automated applications — the same count its subject
   got from 300 manual ones.

**Design changes to adopt** (complaints we would otherwise inherit):

1. **User data lives outside the install** from day one — career-ops keeps
   personal data inside the repo checkout, making branch switches and updates
   risky (#524, open, users asking for a data-dir variable).
2. **Enforce CV faithfulness, don't prompt it.** career-ops' anti-fabrication
   rule exists only as prose and drifted out of 13 locale files (#1411, #2573);
   ai-job-search's agent piggybacked on prior tailored resumes, compounding
   exaggeration across a session (#177). We need a deterministic
   verify-against-master gate, and tailored output must never feed back as
   source.
3. **Loud, inspectable fetch health.** The most-duplicated AIHawk failure is
   silent: the bot logged success without applying (#852, #919 with 8+ sibling
   issues). Per-source status, last-success timestamps, and a dry-run mode
   (AIHawk users begged: #478, #519) belong in the CLI from v1.
4. **Near-zero-keystroke application capture.** The top answer in a 96-comment
   r/cscareerquestions thread on tracking is "apply then forget" (69 points);
   trackers lose to inertia, not to each other. Logging an application must cost
   almost nothing (URL paste-parse, MCP call from the agent that just applied).
   Bonus concrete use case: a user keeps a spreadsheet because Washington State
   unemployment requires a personal application log — make export a first-class
   verb.
5. **No 0–100 match score.** Recruiters publicly debunk Jobscan-style
   percentages ("not looking for a percentage or numerical score at all"); users
   resent chasing scores that reward adding "friendly" to a resume. One asked to
   "just get the keywords and vet them" — surface unranked keyword evidence
   instead.
6. **Plan coverage beyond the big three.** HiringCafe is criticized for exactly
   the skew our Greenhouse/Lever/Ashby scope has (mid/large tech, weak on small
   startups and non-tech). whenthisjobwasposted.com's 37-ATS cascade is a
   feasibility proof and priority queue; add a generic JSON-LD `JobPosting`
   fallback and make coverage visible.
7. **Distrust employer-declared fields.** A posting filed under "Berlin" whose
   description said "fully onsite... office in Lisbon" (HN); LinkedIn's remote
   filter passes city-tagged hybrid jobs, spawning a workaround-guide cottage
   industry. Lint description text against declared fields and flag
   contradictions; treat our own diff history, not employer metadata, as ground
   truth.
8. **Honest left-censoring.** Several ATSes publish no posted date; our
   `first_seen` starts when watching starts — label it a lower bound, and note
   LinkedIn's "posted X ago" is often a repost/refresh date.
9. **Token-budget UX even without scanning.** LLM evaluation still costs; expose
   per-run token estimates and batch limits (career-ops' mitigation was a batch
   --limit flag).
10. **Agent-agnostic from the start.** ai-job-search closed OpenCode support as
    not planned; its fork-and-customize model leaves forks 14 commits behind
    with manual cherry-picking (#300). MCP-first is the portable surface;
    Claude-only skills are a documented churn source.

**Traps** (whole categories of complaint to stay out of):

- Scraping LinkedIn/Indeed/Glassdoor — AIHawk's ban issues (#81, #160, #573),
  selector rot (8+ "not applying" duplicates), and its maintainer's pivot to
  fingerprint-evasion tooling show this treadmill consumes projects. All three
  sites also bot-block the ghost-checker tools.
- Auto-apply or agent-driven form filling — misfill horror stories (ethnicity
  submitted in a country field, AIHawk #826; LoopCV cold-emailing CEOs about
  closed reqs), plus the HN backlash. Stay review-and-decide; never claim
  "auto-apply."
- Binary ghost verdicts — legitimately long-open high-turnover roles generate
  false positives with real cost (a detector author refuses verdicts for this
  reason). Show evidence: age vs company baseline, repost lineage, diffs.
- Crowd-sourced ghost reporting — ghostjobs.io-style apps die of empty-room
  adoption ("Nobody used it").
- Fragile toolchain dependencies — ai-job-search's first-ever issue was a LaTeX
  class error, still recurring 4 months later (#1, #242); vendor or avoid system
  packages in any PDF pipeline.
- Hidden paid dependencies — career-ops' EXA websearch requirement pushes users
  to a paid API key; closed as not planned (#277). Everything in our hot path
  must be free or optional.

## 1. Big Claude Code repos (career-ops 63.5k★, ai-job-search 31.1k★)

Verified from their own issue trackers, August 2026. Token cost is career-ops'
confirmed top ask (roadmap #1226: cost reduction is "the dimension we're leaning
into hardest"; 11 rocket reactions). Storage integrity: #1623 (wrong report
rendered when tracker and report counters drift), #2385 (filesystem-bound report
reads), ai-job-search #298 (enum spelled 5 ways; declined offers vanish from
reports), #269 (applications untracked across five write paths). Reliability:
scan returned only expired listings for a Singapore user (#373); a provider
404'd (#2494); fresh-browser sessions hit LinkedIn login walls and CAPTCHAs
every run (#238). Hallucination: 27 issues match fabrication/hallucination
searches in career-ops; both repos have open or recently-fixed resume-drift
issues (#1411, #2677, #177). Ghost jobs: discussion #140 documents unmet demand
— the maintainer recommends coping (a 30-minute daily cap) because he couldn't
build the signal.

## 2. Auto-appliers (AIHawk, LazyApply, Sonara, LoopCV, Simplify Copilot)

AIHawk: 692 lifetime issues; the most-reacted one is the bot silently not
applying while recording success. Ban reports cluster in Aug–Oct 2024; the
repo's auto-apply code was later removed (#1084: "this repo is misleading"),
plugins pulled over copyright. 404 Media's 2,843-application test found
wrong-country answers and duplicate cover letters to one company. LazyApply:
about 2.2/5 on Trustpilot, 57% one-star; recurring: cannot fill name fields,
CAPTCHA failures, the 30-day money-back guarantee reported unenforceable.
Sonara: pre-shutdown it sent 15+ applications to the same job in different
cities; its shutdown stranded user history. LoopCV: cold emails "went direct to
CEOs and half weren't even open job reqs" (Reddit via Adzuna's 2025 roundup).
Simplify Copilot: the free autofill is liked (4.9/5, 3.8k ratings, 500k users)
but ATS coverage holes (Workday complaints, ~40–50% field accuracy on
iCIMS/Taleo per testers), "glorified autofill" value ceiling (subreddit
criticism, reported via aggregators — upvote count not independently verified),
and a paid tier near 3.0/5 with billing complaints.

## 3. Hosted trackers (Teal, Jobscan, Simplify, Huntr, Careerflow)

Teal: paid AI called generic in the canonical r/jobsearchhacks thread ("ChatGPT
just as effective," refund demands); extension "doesn't even work on Workday,"
and one user got a LinkedIn automation warning for having the Teal plugin
installed; PDF-only export confirmed twice, three years apart; Trustpilot
billing complaints (charges after cancellation, subscription traps) plus "the
job listings on their site feel like mostly ghost listings." Jobscan:
match-score chasing debunked by recruiters in-thread; "50+ jobs and zero
interviews"; billing is the loudest Trustpilot theme (renewals without notice —
about $71–96 reported — trial charges inside 24 hours, charges after
cancellation); core scan broken for 30 days with canned support replies.
Simplify: AI tailoring "will lie by default"; non-US users report most features
unsupported with refunds refused; support forum published users' private emails.
Huntr: thin complaint surface; lock-in gripe (works only with resumes built on
their platform) and price. Careerflow: no self-serve account deletion (two
independent reviews, May 2025 and May 2026). Cross-tool: the abandonment pattern
— "apply then forget" beats every tracker.

## 4. Search engines and ghost-job tools (HiringCafe, LinkedIn/Indeed, checkers)

Confidence note: Reddit was unreachable for this cluster; r/hiringcafe evidence
is secondhand via dated review roundups (Hyrre, Scoutify; Jobright is a
competitor — discounted) plus firsthand HN. HiringCafe patterns: stale filled
listings staying indexed 60–90 days; twice-daily update cadence with delayed
alerts; filter/metadata bugs (filter says seven jobs, two exist; the
Berlin/Lisbon location case); coverage skew to mid/large tech; and no post-apply
workflow anyone praises. LinkedIn/Indeed: reposts and ghost listings dominate
(HN threads at 394/446/225/209 points; Blind users report majority-repost search
results); promoted-job spam survives filters; the remote filter structurally
passes hybrid jobs; "verified" badges draw skepticism ("trust crowd-sourced red
flags over the company-sourced blue badge"). Ghost checkers:
whenthisjobwasposted.com documents its own limits — stateless, one URL at a
time, Wayback first-capture lags actual posting, employer metadata manipulable,
several ATSes publish no dates; heuristic browser extensions have ~40 users; a
Show HN ghost-jobs app died of non-adoption.

## Verification notes

All GitHub issue citations fetched and confirmed (titles, quotes, states);
reaction counts unconfirmed where GitHub's HTML omits them. Corrections applied:
Adzuna's LoopCV roundup is 2025, not 2026; the Wired/LazyApply story's
aggregator link failed DNS — corroborated via Futurism/Yahoo; AIHawk #826's body
is an empty template (claim rests on the title); Jobscan's exact
"$96" renewal figure wasn't surfaced in snippets ($71 was) — softened above;
Simplify's "glorified autofill" upvote count is aggregator-reported only. Reddit
thread quotes for the hosted-tracker cluster were verified verbatim against the
Arctic Shift archive; archived vote counts drift below live ones.

## References

### Agent repos

- [Token Usage #98](https://github.com/santifer/career-ops/issues/98) — 18% of a
  Claude Pro 5-hour budget in 11 minutes.
- [Discussion #1089](https://github.com/santifer/career-ops/discussions/1089) —
  ~500k tokens, 4-hour lockout; DOM-in-context diagnosis.
- [Discussion #410](https://github.com/santifer/career-ops/discussions/410) —
  "That cost me $20 USD in tokens."
- [Issue #1623](https://github.com/santifer/career-ops/issues/1623) — wrong
  report rendered when row counters drift.
- [Issue #2385](https://github.com/santifer/career-ops/issues/2385) — report
  reading 70%+ filesystem syscalls.
- [Issue #524](https://github.com/santifer/career-ops/issues/524) — user data
  inside the repo checkout; data-dir request.
- [Issue #373](https://github.com/santifer/career-ops/issues/373) — expired
  listings surfaced as live.
- [Issue #238](https://github.com/santifer/career-ops/issues/238) —
  fresh-browser CAPTCHA/login pain.
- [Issue #277](https://github.com/santifer/career-ops/issues/277) — EXA paid
  dependency; closed not planned.
- [Issue #1411](https://github.com/santifer/career-ops/issues/1411) —
  fail-closed CV faithfulness gate proposal.
- [Roadmap #1226](https://github.com/santifer/career-ops/discussions/1226) —
  cost reduction top ask; anti-scam detection unbuilt.
- [Issue #2](https://github.com/MadsLorentzen/ai-job-search/issues/2) —
  "consumed all my tokens on a couple simple commands."
- [Issue #298](https://github.com/MadsLorentzen/ai-job-search/issues/298) —
  status enum spelled 5 ways; silent report drops.
- [Issue #269](https://github.com/MadsLorentzen/ai-job-search/issues/269) — six
  readers, one writer; vanishing applications.
- [Issue #177](https://github.com/MadsLorentzen/ai-job-search/issues/177) —
  compounding resume drift across a session.
- [Issue #242](https://github.com/MadsLorentzen/ai-job-search/issues/242) —
  LaTeX moderncv compile failures (with #1, #233).
- [Issue #300](https://github.com/MadsLorentzen/ai-job-search/issues/300) — fork
  14 commits behind; cherry-pick triage.
- [#140](https://github.com/MadsLorentzen/ai-job-search/discussions/140) —
  discussion; maintainer: repost cycles "never certainty."
- [HN: spam thread](https://news.ycombinator.com/item?id=48370330) — 969 points
  against automated job outreach.

### Auto-appliers

- [#919](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk/issues/919) —
  AIHawk; silent not-applying loop; most-reacted issue.
- [#852](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk/issues/852) —
  AIHawk; success.json written without applying.
- [#81](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk/issues/81) —
  AIHawk; LinkedIn ban reports (with #160, #573).
- [#826](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk/issues/826) —
  AIHawk; ethnicity submitted in country field (title-only).
- [#1084](https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk/issues/1084)
  — AIHawk; auto-apply code removed; "misleading."
- [HN: AIHawk](https://news.ycombinator.com/item?id=41756371) — 81 points;
  fabricated-resume and arms-race criticism.
- [404 Media test](https://www.404media.co/i-applied-to-2-843-roles-the-rise-of-ai-powered-job-application-bots/)
  — 2,843 applications; misfills (partial paywall).
- [LazyApply reviews](https://www.trustpilot.com/review/lazyapply.com) — about
  2.2/5, 57% one-star (via snippets).
- [Wired via aggregator](https://metanews.com/man-applies-for-5000-jobs-using-ai-lands-20-interviews/)
  — 5,000 apps, 0.4% interviews (DNS flaky; corroborated by Futurism/Yahoo).
- [Sonara shutdown](https://www.resumly.ai/answers/what-happened-to-sonara-ai) —
  Feb 1, 2024 shutdown stranded user history.
- [Sonara review](https://www.tealhq.com/post/sonara-review) — 15+ duplicate
  applications to one job.
- [LoopCV](https://www.adzuna.com/blog/loopcv-review-and-the-best-alternatives/)
  — Adzuna roundup, October 2025; CEO cold-email quote.
- [LoopCV reviews](https://www.trustpilot.com/review/loopcv.pro) — ~3.9/5,
  polarized (via snippets).
- [Simplify Copilot reviews](https://chromewebstore.google.com/detail/simplify-copilot-autofill/pbanhockgagggenencehbnadejlgchfc/reviews)
  — 4.9/5, 3.8k ratings; Workday complaint verbatim.
- [Simplify review](https://www.resumly.ai/answers/simplify-jobs-review) —
  Simplify+ billing complaints; iCIMS/Taleo accuracy.
- [Blind: bots](https://www.teamblind.com/post/massive-lazyapply-and-other-job-application-bots-how-good-is-it-zjauq6wr)
  — "tons of garbage applications" (via snippets).

### Hosted trackers

- [Teal+](https://www.reddit.com/r/jobsearchhacks/comments/16mvghr/teal/) —
  r/jobsearchhacks; generic AI, refund demands (archive-verified).
- [r/jobs: Teal price](https://www.reddit.com/r/jobs/comments/15xugah/how_much_is_tealhqs_price_paidtime_and_hassle/)
  — Workday breakage, PDF-only export, LinkedIn warning.
- [r/linkedin: Teal hype](https://www.reddit.com/r/linkedin/comments/1b28q4l/linkedin_influencers_pushing_teal_software_is_it/)
  — influencer-marketing backlash.
- [Teal reviews](https://www.trustpilot.com/review/tealhq.com?stars=1&stars=2) —
  Trustpilot; billing, export, ghost-listing complaints.
- [r/recruitinghell: Jobscan](https://www.reddit.com/r/recruitinghell/comments/192gma6/is_jobscan_worth_it/)
  — charges without notice; forced template.
- [r/resumes: ATS scores](https://www.reddit.com/r/resumes/comments/14pwu6r/are_ats_test_sites_like_jobscan_worth_it_and_how/)
  — recruiters debunk match percentages.
- [Reviews](https://www.trustpilot.com/review/jobscan.co?stars=1&stars=2) —
  Jobscan Trustpilot; renewal/trial billing complaints.
- [r/csMajors: Simplify+](https://www.reddit.com/r/csMajors/comments/1aier4e/does_anyone_use_simplify_premium_is_it_worth/)
  — buggy tailoring; EU features unsupported.
- [Simplify Trustpilot](https://www.trustpilot.com/review/simplify.jobs) — "AI
  will lie by default"; board ran dry in ~13 applications.
- [Huntr](https://www.trustpilot.com/review/huntr.co?stars=1&stars=2&stars=3) —
  Trustpilot; own-builder lock-in quote.
- [r/cscareerquestions: tracking](https://www.reddit.com/r/cscareerquestions/comments/1aomsot/how_do_you_keep_track_of_your_job_applications/)
  — "apply then forget"; WA unemployment log use case.
- [Careerflow Trustpilot](https://www.trustpilot.com/review/careerflow.ai?stars=1&stars=2&stars=3)
  — no self-serve account deletion.

### Search engines and ghost tools

- [HN: Hiring Cafe](https://news.ycombinator.com/item?id=42803304) — launch
  thread; filter-count bug, promoted-spam quotes.
- [Scoutify review](https://scoutify.com/blog/hiringcafe-review/) — 2x/day
  cadence, coverage skew (roundup, medium confidence).
- [Hyrre roundup](https://www.hyrre.me/blog/hiring-cafe-reviews) — stale
  60–90-day listings, duplicates (roundup, medium confidence).
- [Jobright review](https://jobright.ai/blog/hiringcafe-review-2026-features-pros-cons-and-alternatives/)
  — competitor-authored; discounted.
- [Ask HN: job search](https://news.ycombinator.com/item?id=42623386) —
  years-long reposts; wrong-tech filter results.
- [HN: ban ghost jobs](https://news.ycombinator.com/item?id=45028785) — 446
  points (with 394/225/209-point siblings).
- [HN: NY ghost-job bill](https://news.ycombinator.com/item?id=48558338) —
  regulatory momentum.
- [1-in-5 study](https://www.aol.com/rise-ghost-jobs-one-five-180917662.html) —
  Greenhouse study via AOL: 1 in 5 postings fake or never filled.
- [Blind: reposts](https://www.teamblind.com/post/open-positions-reposted-for-months-and-not-filled-bdraswgr)
  — majority-repost search results (via search).
- [Remote-filter workarounds](https://theremotehive.com/remote-jobs-linkedin/) —
  cottage industry around LinkedIn's broken filter.
- [whenthisjobwasposted](https://whenthisjobwasposted.com/about) — source
  cascade and self-documented limits.
- [Skip This Job extension](https://chromewebstore.google.com/detail/skip-this-job-%E2%80%94-ghost-job/nodldfdkjomniknohmejdimjlejfongd)
  — 40 users; heuristic ceiling.
- [DEV: detector author](https://dev.to/_350df62777eb55e1/ghost-jobs-are-wasting-your-time-i-built-a-chrome-extension-to-detect-them-1pm6)
  — refuses binary verdicts; false-positive cost.
- [Show HN: ghostjobs.io](https://news.ycombinator.com/item?id=43853401) —
  "Nobody used it" adoption warning.
