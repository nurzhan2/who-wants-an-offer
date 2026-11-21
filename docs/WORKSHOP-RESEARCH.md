# Исследование для мастерской (21.11.2026)

Откуда взялись правила и образцы в `backend/app/workshop/seed/`. Сделано по
просьбе владельца: «найди 30+ источников и 50 референсов топ-разработчиков».

- **Источники:** 38 прочитаны целиком (31 англоязычный, 7 русскоязычных), список ниже.
  Сеть сессии пускала только на GitHub, поэтому Habr, hh.ru и Pragmatic Engineer
  учтены лишь по поисковой выдаче (раздел 1b) и в счёт не идут.
- **Резюме:** 52 открытых резюме разработчиков (их авторы сами опубликовали их на
  GitHub) и 8 антипримеров. Ссылки сюда не включены: в адресах есть имена людей.
  LinkedIn не использовался — правила проекта запрещают автоматический сбор оттуда.
- **Что из этого сделано:** 88 правил в `seed/rules.json` (часть из 51 правила
  ниже, которую умеет проверять мастерская: запрещённые фразы и длина), 4
  обезличенных образца в `seed/references.json`, новый промпт письма
  (`backend/app/llm/prompts/cover_letter.md`): без самокритики, 600–1000 знаков,
  порядок «роль → кто → 1–3 доказательства → следующий шаг».

## 1. Sources (38 opened: 31 English or international, 7 Russian)

| # | Title | Author / org | URL | Key takeaway |
|---|---|---|---|---|
| S1 | Practical guide to writing FAANG-ready software engineer resumes | Tech Interview Handbook (Yangshun Tay, ex-Meta interviewer) | https://github.com/yangshun/tech-interview-handbook/blob/HEAD/apps/website/contents/resume.md | Use an ATS-safe layout: standard headings, standard fonts, 10pt minimum, 1 page. Write a headline under 10 words and a summary under 50 words. Date jobs as `MM/YYYY`. Bullets follow "[Accomplishment]: [Action] that resulted in [quantifiable outcome]". Mirror JD keywords and spell out acronyms. A cover letter should highlight the resume, not repeat it, and stay under a page. |
| S2 | Resume overview ("The 10 seconds glance") | Christina Ng (recruiter), in Tech Interview Handbook | https://github.com/yangshun/tech-interview-handbook/blob/HEAD/apps/website/contents/resume-old.md | Recruiters keyword-match against must-have / good-to-have / bonus checklists in about 10 seconds. If it takes longer than that to work out what you did, the resume fails. Honesty is the most important criterion. |
| S3 | Resume101 | rezzminator | https://github.com/rezzminator/resume101 | Use an exact role title, not a generic "Software Engineer". Put skills right after the summary and leave out soft skills. Bullets are past-tense action plus impact. Lists good and bad bullet examples ("Worked on web development projects" is bad). Note: its sample summaries use "Results-driven… team player", which other sources ban (S20, S19). |
| S4 | Awesome Resumes (docs: structure, common mistakes, ATS, skills, work experience, languages) | Mani Mozaffar | https://github.com/ManiMozaffar/awesome-resumes | 475–600 words, 1 page under 7–10 years of experience, `MM/YYYY` dates, 2 to 6–7 bullets per job. Use the JD's exact spelling ("React.js", not "Re-act"). Skills list only what you can talk about for 15 minutes, with no self-ratings and no soft skills. List languages with standard proficiency labels. Don't invent metrics: "you're responsible for all numbers". |
| S5 | ATS-Friendly Resume Tips | Aman Chowdhury | https://github.com/aman0046/ATS-Friendly-Resume-Tips | Include only name, email, phone, location and links. Use standard section headers. Must-have sections are Education, Experience, Skills, Achievements, Projects. Leave out the objective section. Use bullets, not paragraphs, and put numbers in them. |
| S6 | Game Programmer Resume Tips (author's own 2010/2013/2016 resumes, critiqued with hiring manager Mike Acton) | Dale Kim | https://github.com/unpacklo/game-programmer-resume-tips | A hiring manager calls back about 10 resumes per posting, and each resume gets about 20 seconds. Bullets like "Worked on X / Provided input / Developed tests" mean "I programmed stuff". Give context, constraints and numbers with their conditions (for example "5 ms frame time on <hardware>"). Coursework and hobbies add almost nothing. |
| S7 | Hiring-manager outreach prompt (Career playbooks for software engineers) | asmbld (written from 10 years of tech recruiting) | https://github.com/hngotech/career-playbooks-for-software-engineers/blob/HEAD/prompt-message-to-hiring-manager.md | Cold message of 5–7 sentences at most. Reference something specific about the company in the first 2 sentences. Summarise your experience in one sentence instead of listing skills. Make a small ask. Don't apologise for reaching out. |
| S8 | 3.1 Tech Resume Building | Amherst College Loeb Center (college career office) | https://github.com/Amherst-College-Loeb-Center/Tech-Industry-Resources/blob/HEAD/3-Crafting-Application-Materials/3.1-Tech-Resume-Building.md | 1 page, PDF, header with LinkedIn and GitHub and no mailing address. Bullets are "what you did, why, and metrics". Don't exaggerate. Proofread. |
| S9 | 3.2 Tech Cover Letter Drafting | Amherst College Loeb Center | https://github.com/Amherst-College-Loeb-Center/Tech-Industry-Resources/blob/HEAD/3-Crafting-Application-Materials/3.2-Tech-Cover-Letter-Drafting.md | Structure: intro (role, how you found it), relevant experience, why this company (specific projects or technologies), contribution, call to action. Under one page. Don't repeat the resume or paste phrases from the JD. |
| S10 | 4.1 Making Connections (cold email) | Amherst College Loeb Center | https://github.com/Amherst-College-Loeb-Center/Tech-Industry-Resources/blob/HEAD/4-Networking/4.1-Making-Connections.md | Personalise every cold email and include 1–2 specific questions. Follow up once after a week. Recruiter DM template: who you are, one concrete project, the ask. |
| S11 | Resume Best Practices (2025/2026) | Stephanie Koehl | https://github.com/StephanieKoehl/resume-best-practices | 7.4-second first scan. Single column; no tables, columns, icons or skill bars. 3–4 bullets for the latest role, never more than 5–6. Each bullet 1–2 lines. Uses Google's XYZ formula ("Accomplished X as measured by Y by doing Z"). Ban "Helped / Assisted / Participated in / Was responsible for / Worked on", objective statements, "References available upon request" and first-person pronouns. Estimate conservatively with "~". |
| S12 | What startup recruiters actually see when you apply for a job | Charles Cook, PostHog (in-house recruiter) | https://github.com/PostHog/posthog.com/blob/HEAD/contents/blog/what-recruiters-see.md | About 460 applicants per role and under 1 minute per application. Write a short, obviously personalised cover letter of **5–10 sentences** on why this role. "Hi I'm X" wastes words. Match the company's tone (no "Dear Sir/Madam" at informal companies). No obvious ChatGPT text. Use a plain CV with no photo and keep it current. Don't list every language without showing which one you're strong in. |
| S13 | Engineering hiring (handbook) + hiring process index | PostHog | https://github.com/PostHog/posthog.com/blob/HEAD/contents/handbook/people/hiring-process/engineering-hiring.md | "We don't care how many years of professional experience someone has." They look for: picks up stacks quickly, built something from scratch, strong written communication, has built things agents use (MCP servers, evals). A resume not customised to the role is a red flag. |
| S14 | Early-career Engineering Hiring | Sourcegraph handbook | https://github.com/sourcegraph/handbook/blob/HEAD/content/departments/engineering/dev/hiring/early-career-engineers.md | Defines early career as under 12–24 months of experience. Advises managers to focus on skills rather than years and notes that stating years in a JD scares candidates off. Years requirements are softer than they look. |
| S15 | Writing a federal government resume | 18F (Amanda Costello) | https://github.com/18F/handbook/blob/HEAD/pages/hiring-staying-or-changing-jobs/resume.md | The resume's job is "to connect the requirements of the job posting to your experience using the straightest possible line". Highlight every key phrase in the posting and make sure each one is covered. (Its federal length norms don't apply here.) |
| S16 | Apply Within skill: resume and cover-letter references | tomigbel (unofficial study aid of Laszlo Bock's "Apply Within" playbook; Bock was Google SVP People Ops) | https://github.com/tomigbel/apply-within | X/Y/Z formula "Accomplished [X] as measured by [Y] by doing [Z]", with original, better and awesome rewrites. PDF, 11pt, no tables or columns, mirror JD phrases, spell out acronyms. Cover letter: 4 paragraphs (1-sentence intro, ≤3 sentences expanding, ≤3 sentences on why this company specifically, follow-up). Address a named person, because "Dear Hiring Manager" is about 30% more likely to be rejected. |
| S17 | McDowell CV (LaTeX template of Gayle Laakmann McDowell's resume design, careercup.com/resume) | dnl-blkv / G. L. McDowell | https://github.com/dnl-blkv/mcdowell-cv | McDowell's own sample: one page, Employment then Education then Projects then Awards then Languages and Technologies. Results-first bullets ("Reduced time to render … by 75% by implementing prediction algorithm"). |
| S18 | Coding Interview University: "Update Your Resume" | John Washam | https://github.com/jwasham/coding-interview-university | Points to McDowell's "This Is What A GOOD Resume Should Look Like", with the author's caveat that it is US-focused and other countries' CVs differ. Relevant to RU vs EN differences. |
| S19 | Resume help | SixArm (hiring checklist) | https://github.com/SixArm/resume-help | Prioritise; summarise fit with the specific job; no objective statement. Prove worth with metrics. Show your top 3 work samples. Use keywords, not empty words ("detail oriented", "team player", "hard working"). One page, bullets, PDF, proofread. |
| S20 | career-ops: `modes/_writing.md` and `cv-voice.mjs` (BANNED_PHRASES) | career-ops (open-source job-search agent, about 84k stars) | https://github.com/career-ops-hq/career-ops | Machine-enforced cliché ban list ("passionate about", "results-driven", "proven track record", "leveraged", "spearheaded", "facilitated", "synergy", "cutting-edge", "best practices", "utilized", "delve", "pivotal"…). No em dashes. Vary bullet openers. "Cut p95 latency from 2.1s to 380ms" beats "improved performance". |
| S21 | career-ops: `modes/cover.md` | career-ops | https://github.com/career-ops-hq/career-ops/blob/HEAD/modes/cover.md | Cover letter: active voice only. No filler openers ("I am writing to express", "I am excited to"). Also bans "perfect fit", "unique opportunity", "excited", "strong track record". Every claim needs a number, a system name or an outcome. Mirror JD vocabulary once, without stuffing. Formal letter 350–420 words. Ask the candidate about gaps instead of auto-inserting them. |
| S22 | career-ops: `modes/heuristics/recruiter-side.md` | career-ops | https://github.com/career-ops-hq/career-ops/blob/HEAD/modes/heuristics/recruiter-side.md | Six-second clarity gate: the top third must show target role, matching stack, one production outcome and remote fit. Bullet = "Action + system/scope + tool/approach + outcome + proof". For seniority doubts: "lead with senior behaviors, not tenure alone". Never invent evidence. Answer logistics only where asked. |
| S23 | career-ops: `modes/contacto.md` (LinkedIn/DM outreach) | career-ops | https://github.com/career-ops-hq/career-ops/blob/HEAD/modes/contacto.md | Recruiter DM in 3 sentences: Fit (role, experience, availability, location), then Proof (data that answers their screening questions in advance), then CTA ("Happy to share my CV if this aligns"). |
| S24 | Resume Parser Algorithm Deep Dive | OpenResume (Xitang Zhao) | https://github.com/xitanggg/open-resume/blob/HEAD/src/app/resume-parser/ResumeParserAlgorithmArticle.tsx | How an ATS-style parser reads a PDF. It is designed for single-column English resumes. A section title is detected when it sits alone on a line, bold, in ALL CAPS, with a fallback match on common section keywords. Non-standard headings break section detection. |
| S25 | JSON Resume schema | jsonresume.org | https://github.com/jsonresume/resume-schema/blob/HEAD/schema.json | The de facto standard section set: basics, work, volunteer, education, awards, certificates, publications, skills, languages, interests, references, projects. Use these names for detectable sections. |
| S26 | Common Resume Mistakes (resume-builder-skill) | dabydat (summarises Harvard Career Services, r/EngineeringResumes, The Tech Resume Inside Out) | https://github.com/dabydat/resume-builder-skill/blob/HEAD/skill/references/common-mistakes.md | Harvard's top 5 mistakes: typos, missing email or phone, passive language, hard to skim, no results. The "responsibility resume" is the most common mistake. Missing domain context comes second. Single column, no graphics or tables. |
| S27 | RESUME_GUIDE.md | Hozaifa Moustafa | https://github.com/HozaifaMoustafa/hozaifamoustafa-resume-latex/blob/HEAD/RESUME_GUIDE.md | 0–3 years means 1 page, strictly. Format: "Job Title, Company, City, Country, Month Year – Month Year". Use 3–5 bullets for recent roles. Past tense for past roles. City and country only. No photo, date of birth, marital status or nationality. Phone with country code for international applications. |
| S28 | workat.tech sample resume (README) | workat.tech (sample of an SDE's real resume) | https://github.com/workattech/workattech_sample_resume | Always exactly one page. As new items arrive, older ones get compressed and the least relevant are removed. Small projects drop off after the first real job. |
| S29 | The Hitchhiker's Guide to Job Searches | Jim Zucker | https://github.com/jimzucker/hitchhikers-guide-jobsearch | Recruiters search first. Pick ONE standard job title and use it exactly. ATS cares about exact title and keyword matches. |
| S30 | br-dev-remoto (finding foreign remote jobs from Brazil; Portuguese) | Nigel Lima | https://github.com/nigellima/br-dev-remoto | For candidates outside the hiring country: a 1-page English CV with highlight bullets ("Increased the API performance by 10%"). Translate LinkedIn and GitHub to English. Keep a roughly 4-paragraph cover letter ready for forms and email bodies. The first call also tests English. |
| S31 | Awesome Tech Immigration: Resume & CV general rules | Tech Immigrants community | https://github.com/TechImmigrants/awesome-tech-immigration | No age, gender, photo (unless the country requires one), marital status or full address. Contact info only. Tailor every CV to JD keywords. Quantify. One page under 10 years. PDF. |
| S32 | newresume: «конструктор сопроводительного письма» (letter.docx) and «Структура резюме» (diagram) | yaesya | https://github.com/yaesya/newresume | RU. Letter skeleton: Приветствие, О себе (recent results), Мотивация (why this company, something specific), Заключение (resume attached, contact). «Не используй клише… не пиши, что ты "ответственный, коммуникабельный, исполнительный, трудолюбивый, честный"». The diagram says: no template objective, achievements as "сделал это, выручка увеличилась на 123%", «Убери шаблонные фразы! Будь живее!», plus a self-check list. On hh.ru a business photo and desired salary are expected. |
| S33 | Career track, Project 02: резюме, сопроводительное письмо, зарплата | School 21 (Sber's «Школа 21»), published by esylva | https://github.com/esylva/21_School_Career_track/blob/HEAD/Project%2002/README.md | RU. Contacts should include a Telegram nick, because «многие рекрутеры используют телеграм для первичного общения». Experience in reverse order. Dates as month and year only («март 2018 – апрель 2020», «по наст. время»). Results must be quantified («сэкономить до 60 чел/часов в месяц»). Don't copy the job description. In «О себе», don't list «стрессоустойчивый, проактивный, коммуникабельный» separated by commas. At most 2 pages, ideally 1. Letter structure: greeting, position and source, why the company, strengths, motivation, contacts. |
| S34 | Career track, Project 01: анализ вакансий, hard/soft skills | School 21, published by esylva | https://github.com/esylva/21_School_Career_track/blob/HEAD/Project%2001/README.md | RU. Build a table of target vacancies and count the most frequent requirements. Use it to choose and order skills and to decide what experience to emphasise. |
| S35 | «План резюме» (questionnaire) | teacher-army | https://github.com/teacher-army/awesome-resume | RU. Write down your stack first and weave it into the story. Cover hard tasks you solved and how, your process and practices, and your pet projects. It produces the material for «О себе» or a letter instead of a list of adjectives. |
| S36 | career-ops RU mode: `modes/ru/_shared.md` | career-ops | https://github.com/career-ops-hq/career-ops/blob/HEAD/modes/ru/_shared.md | RU. Clichés to avoid: «Я увлечён…», «результат-ориентированный», «обширный опыт», «синергия», «комплексный», «инновационный», «в современном быстро меняющемся мире». Specifics: «Снизил p95 latency с 2.1с до 380мс». Always clarify gross vs net when salary comes up. |
| S37 | hh-applicant-tool README: cover-letter prompt and config | s3rgeym (most-starred open-source hh.ru automation tool) | https://github.com/s3rgeym/hh-applicant-tool | RU. Default hh.ru cover letter is **4–6 предложений**: first person, friendly and businesslike, «без шаблонных фраз вроде "стрессоустойчивый, целеустремленный"», no markdown, no placeholders, no invented facts, and the name is the only signature. |
| S38 | hhru-bot `ai/letters.py` (system prompt) | axisrow | https://github.com/axisrow/hhru/blob/HEAD/src/hhru_bot/ai/letters.py | RU. hh.ru letters should be «короткие… вежливо, без воды и без выдуманных фактов о кандидате. Только текст письма». Use the vacancy title and company, and the start of the vacancy description carries the useful signal. |

### 1b. Seen only as search-engine extracts (NOT opened, NOT counted; weak corroboration)
- Habr resume articles for developers (via search): https://habr.com/ru/post/667284 , https://habr.com/ru/post/474894 , https://habr.com/ru/articles/933822 (a recruiter's «семь смертных грехов IT-резюме»). Extracted points: name the stack, frameworks, tests and tasks; tailor keywords per vacancy; Western companies run ATS; don't lie.
- hh.ru articles on cover letters (via search): https://hh.ru/article/21395 , https://hh.ru/article/30827 , https://hh.ru/article/302229 . Extracted points: one sentence on why the vacancy, one paragraph of achievements, don't duplicate the resume, «Добрый день / Здравствуйте», formal-business style. A letter is needed when the vacancy asks for one, for foreign companies, or on a career change. A third-party study of 15k hh.ru replies (quick-offer.ru) claims 250–400-word letters get replies 2.5–3.5 times more often. That conflicts with S37/S38 and was not verified.
- Tproger «Откликнуться на IT-вакансию и запомниться» (https://tproger.ru/experts/otkliknutsja-na-it-vakansiju-i-zapomnitsja-otvechajut-nanimateli): mention the specific vacancy and 1–2 relevant experiences, and put the stack and the resume link in the first lines.
- Gergely Orosz, The Tech Resume Inside Out (via search; freeCodeCamp and Stack Overflow blog summaries): the resume's only goal is to get a recruiter call. Keep it minimal, clear and tailored, with relevant items first.
- Gayle McDowell's "This Is What A GOOD Resume Should Look Like" (via search): one page under 10 years of experience; every line must earn its place.

---

## 3. Distilled checkable rules

Conventions: `scope` ∈ {cv, cover_letter}; `lang` ∈ {en, ru, *}; "cover_letter" covers both the email/application letter and the short recruiter message (`variant: letter` or `variant: dm` for Telegram, LinkedIn DM, or the hh.ru reply text box). Character counts exclude the header/contact block unless stated. Phrase matching is case-insensitive, matches word boundaries, and for RU should be stem-aware (e.g. `ответственн*`).

### 3.1 `length`
| id | scope | lang | rule | justification |
|---|---|---|---|---|
| L1 | cv | en | Total body 350–700 words (about 2,400–5,000 chars); must fit **1 page** | S4 (475–600 words, 1 page), S1, S8, S11, S19, S27 ("0–3 yrs: 1 page strictly"), S31. Exemplars E1/E2/E20 are 500–780 words. |
| L2 | cv | ru | Total body 300–650 words (about 2,300–5,500 chars); 1 page preferred, 2 pages hard max | S33 («не более 2-ух страниц, а лучше одна»), S32 (self-check «Достаточно кратко»), E1/E52 |
| L3 | cv | * | Summary/headline block: headline ≤ 10 words, summary ≤ 50 words (≤ 3 sentences) | S1 (headline < 10 words, summary < 50 words), S11 (2–3 lines), S22 |
| L4 | cv | * | Each bullet 60–240 chars (≤ 2 rendered lines) | S11 ("1–2 lines max; if 3, split or cut"), S6 (enough context, but efficient) |
| L5 | cover_letter (letter) | en | 120–250 words (about 700–1,600 chars); hard max 420 words / 1 page | S12 (5–10 sentences), S16 (4 short paragraphs), S9 and S1 (< 1 page), S21 (350–420 words is the formal upper bound) |
| L6 | cover_letter (dm) | en | 400–900 chars, 3–7 sentences | S7 (5–7 sentences max), S23 (3-sentence Fit/Proof/CTA), S12 |
| L7 | cover_letter (letter, hh.ru reply box or email) | ru | 500–1,500 chars, 4–8 sentences | S37 (4–6 предложений), S38 («короткие… без воды»), S33 (6-part structure); 1b hh.ru extracts (one sentence of motivation plus one paragraph of achievements) |
| L8 | cover_letter (dm, Telegram) | ru | 500–900 chars, 4–7 sentences; ≤ 3 bullets if a list is used | S37, S33 (Telegram is the first-contact channel), S23 |

### 3.2 `forbidden_phrase`
| id | scope | lang | phrases (ban; suggest the replacement in parentheses) | justification |
|---|---|---|---|---|
| F1 | cv | en | "responsible for", "worked on", "helped", "assisted", "participated in", "involved in", "was tasked with", "duties included" (use an action verb plus outcome) | S11, S20/S22 ("helped, assisted, responsible for, worked on, participated in"), S26 ("responsibility resume"), S3, S6 |
| F2 | cv, cover_letter | en | "team player", "hard-working", "hardworking", "detail-oriented", "self-motivated", "fast learner", "quick learner", "go-getter", "think outside the box", "excellent communication skills", "results-driven", "results-oriented", "proven track record", "passionate about", "dynamic", "motivated professional" | S19 (empty words: "detail oriented", "team player", "hard working"), S20 (BANNED_PHRASES), S4 (buzzwords), S1 (avoid clichés) |
| F3 | cv, cover_letter | en | AI-slop / inflated verbs: "leverage(d)", "utilize(d)", "spearheaded", "orchestrated", "championed", "facilitated", "synergy", "cutting-edge", "best practices", "delve", "tapestry", "pivotal", "in today's fast-paced world", "demonstrated ability to", "significant impact", "measurable impact" | S20 (exact list in cv-voice.mjs), S21, S12 ("No ChatGPT, they're really obvious") |
| F4 | cover_letter | en | Filler openers and closers: "I am writing to express", "I am writing to apply", "I am excited to", "I am pleased to", "I hope this email finds you well", "To whom it may concern", "Dear Sir/Madam", "Hi, I'm [Name]" as the opener, "I believe I would be a great fit", "perfect fit", "unique opportunity", "I look forward to hearing from you at your earliest convenience" | S21 (filler openers, "perfect fit", "unique opportunity"), S12 ("hi I'm X" wastes words; no "Dear Sir/Madam"), S7 (no "I hope this isn't too forward") |
| F5 | cv, cover_letter | en | Self-deprecation / volunteered weakness: "I lack", "I don't have", "although I only have", "only X year(s)", "despite my limited experience", "not much experience", "I know I don't meet", "I may not be", "still learning", "junior but", "sorry to bother", "I apologize" | S7 (no apologies), S22 (answer doubts with evidence, never volunteer), S14/S13 (years are a weak filter), S11 (never over-explain gaps on the resume) |
| F6 | cv | en | Section/meta clutter: "Objective", "Career Objective", "References available upon request", "Curriculum Vitae" as the title, "Hobbies" for remote SWE roles | S11 (objective and references are "out"), S5, S19, S6 (hobbies add nothing) |
| F7 | cv | * | Personal data: "Date of birth", "DOB", "Age:", "Marital status", "Nationality", "Gender", "Religion", a full street address, a photo (international), «Дата рождения», «Возраст», «Пол:», «Семейное положение», «Национальность», «Дети» | S31, S27, S12 (no profile pictures), S11; anti-pattern EvgeniiGrinko. Exception: a photo is acceptable only on an hh.ru profile (S32). |
| F8 | cv | * | First-person pronouns in bullets: "I ", "my ", "we " at bullet start; RU «я », «мы » at bullet start | S11 (no "I managed…"), S26 (passive or personal voice); E1 RU style |
| F9 | cv, cover_letter | ru | Personal-quality clichés: «ответственный», «стрессоустойчивый», «коммуникабельный», «целеустремлённый», «исполнительный», «трудолюбивый», «пунктуальный», «честный», «проактивный», «позитивный», «неконфликтный», «обучаемый», «быстро обучаюсь», «быстро осваиваю», «легко обучаем», «нацелен на результат», «результат-ориентированный», «умею работать в команде», «командный игрок», «высокомотивирован», «обширный опыт», «я увлечён», «синергия», «инновационный», «комплексный подход», «в современном быстро меняющемся мире» | S32 («не пиши, что ты "ответственный, коммуникабельный, исполнительный, трудолюбивый, честный"»), S33 («не пиши через запятую: стрессоустойчивый, проактивный, коммуникабельный»), S37 («без шаблонных фраз вроде "стрессоустойчивый, целеустремленный"»), S36; anti-patterns IntroSpace, poymanov, SteSha28 |
| F10 | cv | ru | Duty-style phrasing: «в мои обязанности входило», «обязанности:», «занимался», «участвовал в», «принимал участие», «работал над», «помогал», «отвечал за», «осуществлял» (use perfective past: «Разработал», «Сократил», «Перевёл», «Настроил») | S33 («не рекомендуется прописывать должностные инструкции»; verbs «улучшил, сократил, оптимизировал, разработал»), S32 («только реальные обязанности», achievements as «сделал это… увеличилась на 123%»), E1/E52 |
| F11 | cv | ru | Template objective: «Цель:», «найти компанию, в которой смогу реализовать свой потенциал», «ищу работу в сложном нагруженном проекте для развития» | S32 diagram («Не нужно шаблонных "найти компанию, в которой я смогу реализовать свой потенциал"»), anti-pattern EvgeniiGrinko |
| F12 | cover_letter | ru | Self-deprecation / pleading: «не дотягиваю», «мне не хватает опыта», «у меня пока нет опыта», «у меня небольшой опыт», «хоть я и», «пусть я и», «к сожалению», «не соответствую требованиям», «только начинаю», «понимаю, что не подхожу», «извините за беспокойство», «прошу прощения», «дайте мне шанс» | S7 / S22 (no apologies; answer doubts with evidence), S13/S14 (years are soft), S33 (show «Зачем ты нужен компании?») |
| F13 | cover_letter | ru | Stiff or stock letter phrases, discouraged in Telegram: «Уважаемый работодатель», «Прошу рассмотреть мою кандидатуру» as the whole message, «С надеждой на приглашение», «Буду признательна за прочтение моего резюме», «Мне очень приятно, что Вы откликнулись» | S12 (match the tone; for chat, informal), S38 («без воды»), S37 (friendly-businesslike). These are fine in a formal email on hh.ru (S32 lists them as polite closers), so apply F13 to `variant: dm` only. |
| F14 | cv, cover_letter | * | Placeholders or markdown left in output: `[`, `]`, `{`, `}`, `<`, `>`, `**`, `TODO`, `XX%` | S37 («никаких плейсхолдеров… никакого markdown»), S12 (ATS strips formatting) |
| F15 | cv, cover_letter | * | Typographic tells that hurt ATS: em dash `—` inside sentences (date ranges may use an en dash `–`), smart quotes in the CV, emoji | S20 ("No em-dashes"; normalises smart quotes for ATS), S11 (fancy characters render as garbage). RU: guillemets «» are standard in letters; normalise only in the CV PDF. |

### 3.3 `required_section`
| id | scope | lang | requirement (accepted heading aliases) | justification |
|---|---|---|---|---|
| R1 | cv | en | Header with name, target title, city + country (+ "Remote"/timezone), email, and at least one of GitHub / LinkedIn / portfolio URL. Phone with country code is optional. | S1, S27 (city and country only; country code), S8, S31, E1, E8 |
| R2 | cv | en | **Summary** (alias: headline used as the title, "Profile", "Summary") | S1 (recommended; headline as section title), S11, S22 (six-second gate). S27 says skip under 5 yrs; resolved by keeping it ≤ 50 words. |
| R3 | cv | en | **Skills** (aliases: "Technical Skills", "Skills") | S1, S5, S25, S3 |
| R4 | cv | en | **Experience** (aliases: "Work Experience", "Professional Experience", "Experience") | S1, S5, S24 (standard headings detectable), S25 |
| R5 | cv | en | **Projects** required while commercial experience is under 2 years (aliases: "Projects", "Selected Projects", "Open Source") | S1 ("at least 2 projects"), S5 (must-have), S19 (top 3 work samples), S14 (early career < 12–24 mo) |
| R6 | cv | en | **Education** | S1, S5, S8, S25 |
| R7 | cv | en | **Languages** required for international applications by non-native speakers (English level as CEFR or a standard label) | S4 (languages section, standard labels), S30 (the first call tests English), E1 ("English (C1)") |
| R8 | cv | ru | Header with ФИ (patronymic optional), желаемая должность, город + формат работы, **Telegram**, email, GitHub | S33 (Фамилия Имя; Telegram nick; professional email), S32 |
| R9 | cv | ru | **Опыт работы** | S33, S32 |
| R10 | cv | ru | **Ключевые навыки** or **Навыки** | S33, S34, E11 |
| R11 | cv | ru | **Образование** | S33, S32 |
| R12 | cv | ru | **Проекты** (when commercial experience is under 2 yrs) and **Языки** (when targeting international or English is in the JD) | S33 («Опыт работы без опыта»: projects, open source, hackathons), S32 («Дополнительная информация (языки…)») |
| R13 | cover_letter | * | Must contain: (1) the exact vacancy title or role, (2) ≥ 1 concrete proof with a number or a named system, (3) a link (CV, GitHub or portfolio) or "CV attached", (4) a closing CTA (call, interview, next step) | S9 (role, experience, CTA), S16 (4 parts incl. follow-up), S23 (Fit / Proof / CTA), S33 (position and source, strengths, contacts), S21 (every claim needs a number or system name) |
| R14 | cover_letter | * | Must contain one company- or JD-specific sentence (product, stack line, problem, news) | S12 ("obviously personalized"), S7 (specific thing in the first 2 sentences), S9, S16, S32 (Мотивация: something noticed about the company), S33 (Шаг 1) |

### 3.4 `section_item_count`
| id | scope | lang | rule | justification |
|---|---|---|---|---|
| C1 | cv | * | Bullets per role: 2–6; most recent role 3–5; older or less relevant roles 1–3 | S4 (2 to 6–7), S11 (3–4 recent; never more than 5–6), S27 (3–5 recent; 1–2 older) |
| C2 | cv | * | ≥ 60% of experience bullets contain a number (%, ms, s, ×, count, money, users) | S1, S4, S5 ("use more numbers"), S11, S16 (X/Y/Z), S33 («результаты желательно оцифровать»); E2 and E24 show it's achievable |
| C3 | cv | * | Skills: 8–25 items in 3–6 labelled groups (e.g. Languages / Backend / LLM / Data & Infra / Tooling); no soft skills; no proficiency bars or stars | S1 ("[Skill summary]: [skills]"), S3/S4 (no soft skills, no ratings, avoid listing too many), S12 (don't list every language), S11 (no skill bars) |
| C4 | cv | * | Projects: 1–3 items, each with 1–3 bullets and a link | S1 (≥ 2 with GitHub link), S19 (top 3 samples), S28 (small projects drop off as experience grows) |
| C5 | cv | * | Summary: 1–3 sentences; at least 3 JD keywords (role title plus 2 stack items) | S1, S22 (target role, matching stack, outcome), S29 (exact title) |
| C6 | cv | * | No more than 2 bullets in the same role start with the same verb | S20 ("Don't start every bullet with the same verb") |
| C7 | cover_letter | * | Proof points: 1–3 (quantified); paragraphs: 3–4 (letter) or 2–4 short blocks (dm); links ≤ 3 | S16 (4 paragraphs), S12 (5–10 sentences), S7 (summarise experience in one sentence), S23 (one proof sentence) |
| C8 | cv | * | Languages: 1–4 entries, each with a level (CEFR A1–C2 or Native, or RU «родной / свободный / B2…») | S4 (standard labels, no stars), S33 (honest level list) |

### 3.5 `date_format`
| id | scope | lang | rule | justification |
|---|---|---|---|---|
| D1 | cv | en | Job ranges: `Mon YYYY – Mon YYYY` or `Mon YYYY – Present` (regex `^(Jan\|Feb\|Mar\|Apr\|May\|Jun\|Jul\|Aug\|Sep\|Oct\|Nov\|Dec) \d{4} – ((Jan\|…\|Dec) \d{4}\|Present)$`). `MM/YYYY – MM/YYYY` is also accepted; **one style per document**. | S1 (MM/YYYY), S4 (MM/YYYY throughout), S11 ("Feb 2026 – Mar 2026"), S27 ("Month Year – Month Year") |
| D2 | cv | ru | `месяц ГГГГ – месяц ГГГГ` / `месяц ГГГГ – настоящее время` (accept «н. в.», «по наст. время»). Month in the nominative, lowercase (`(январь\|февраль\|…\|декабрь) \d{4}`). `MM.ГГГГ – MM.ГГГГ` is also accepted; one style per document. | S33 («Указывай только месяцы и года… март 2018 – апрель 2020… по наст. время»), E47 («с 01.2024 по наст. время»), E45 (`05.2022`) |
| D3 | cv | * | No day-level dates (`dd.mm.yyyy`, `2024-03-15`) and no duration-only entries ("8 months") | S33 («Не нужно писать точные даты приема и увольнения»), S11 (use month format, not "5 weeks") |
| D4 | cv | * | Education may be year-only `YYYY – YYYY` (or "Expected YYYY") | S1 (graduation year), S17 |
| D5 | cv | * | The most recent role must end in "Present" / «настоящее время», or the latest end date must be within 3 months of today (no stale CV) | S12 ("Don't have your CV end in 2021… just put [the break]") |
| D6 | cv | * | Roles in reverse-chronological order; dates must not overlap except education with work | S1, S4 (no overlaps except work and education), S33 |

---

## 4. Prose guidance for the model (not checkable)

**4.1 Bullet formula.** Write each bullet as **Result + how it was measured + what you did (with which tech)**. This is Google's X/Y/Z, "Accomplished X as measured by Y by doing Z" (S16, S11), or TIH's "[Accomplishment]: [Action] that resulted in [quantifiable outcome]" (S1). For engineers, add system and scope: *Action + system + tool + outcome + proof* (S22). Lead with a strong past-tense verb (Built, Cut, Migrated, Designed, Shipped, Automated, Reduced). Use present tense only for ongoing duties in the current role (S27). Give enough context for a peer to judge difficulty (data size, traffic, constraints, hardware) (S6). Prefer root cause plus fix ("replaced a loopback HTTP call with an in-process handler", E2) over vague "optimized".

**4.2 Quantification without fabrication.** Use only numbers present in the candidate's facts. Approximate with "~" or "about" when the candidate gives a range (S11). Attribute third-party estimates ("per client estimate", E1). When no number exists, quantify scope instead: count of endpoints, services, docs indexed, models compared, users, tickets, PRs (S11 scale metrics, E3). Never upgrade "helped with" into "led" (S4 #3, S22, S2 "honesty is the single most important criterion"). Good LLM-engineer metrics: latency p50/p95, cost per call or answer, tokens saved, eval pass rate on N real cases, % of tickets or queries handled, retrieval hit rate, number of tools or agents in production.

**4.3 Ordering.**
- **EN CV:** Header, Headline + Summary (≤ 50 words), Skills, Experience, Projects, Education, Languages (S1). Put Education after Experience once there is commercial experience; TIH allows Education first only under 3 years when work is thin.
- **RU CV / hh.ru:** Header (должность, город, формат, Telegram), О себе (2–3 sentences of facts, not adjectives), Опыт работы, Ключевые навыки, Проекты, Образование, Языки (S33, S32).
- **Within a role:** put the bullet most relevant to the JD first (S3, S11).
- **The top third** of the page must answer "role? stack? one production result? remote fit?" (S22, S2's 10-second glance).

**4.4 Tailoring.** Extract 5–10 must-have keywords from the JD (S2's must-have / good-to-have / bonus) and use the JD's exact spelling once each, in context, never as a dump (S1, S4, S21). Reorder Skills to front-load JD items. Choose one exact standard title that matches the JD ("Backend Engineer (Python)", "LLM Engineer") (S29, S3). Mention both forms of key acronyms once: "Retrieval-Augmented Generation (RAG)" (S1, S16).

**4.5 What a cover letter / recruiter message contains, in order.**
- **Email or application letter (EN, 120–250 words):**
  1. Role + one specific detail about the company or JD (no "I am writing to…") (S12, S16, S21).
  2. One sentence of who you are: title + core stack + domain (S7).
  3. 1–3 proofs that map to the JD's main problem, each with a number (S9, S21).
  4. Logistics only if relevant: remote, timezone overlap, English level, start date (S22).
  5. Links (CV, GitHub) and a concrete CTA ("happy to do a technical call this week") (S9, S16).

  Use plain text with line breaks only, because ATS destroys formatting (S12). Match the company's tone: "Hi [Name]," for startups, "Dear [Name]," for corporate. Never "Dear Sir/Madam" (S12, S16).
- **Telegram / LinkedIn DM / hh.ru reply (RU or EN, 400–900 chars):**
  1. Greeting with the recruiter's name.
  2. Which vacancy, and where it was seen (channel or hh link).
  3. One-line identity (title, stack).
  4. 2–3 short proof points (bullets are fine in Telegram).
  5. JD stack overlap in one line.
  6. Format and timezone.
  7. CV link (PDF or hh.ru) and GitHub.
  8. A soft question CTA.

  Put the stack and CV link in the first screen of text (S33, S23, 1b Tproger). One message, not several. No voice messages. No "Привет" without content.

**4.6 Ideal lengths.**

| Format | Length | Sources |
|---|---|---|
| Telegram / DM | 500–900 chars (RU), 400–900 chars (EN) | S7, S23, S37 |
| hh.ru reply text box | 4–6 sentences, ≤ 1,500 chars | S37, S38 |
| Email cover letter (EN) | 120–250 words; ≤ 420 words for formal corporate applications that ask for a full letter | S12, S16, S21 |
| CV | 1 page | L1/L2 |

The 1b claim that 250–400-word letters win on hh.ru is unverified and conflicts with S37/S38. Prefer short.

**4.7 Experience gap (JD asks for 2–3+ years; the candidate has about 1.3).**
- **Never volunteer the gap.** No "although I only have…", no apology, no "I'm a fast learner" (F5, F12; S7, S22).
- **Don't print a small year count** in the summary or letter when it is below the JD's requirement. Show the start date in the experience section and let dates speak (S11 "just let the dates speak"). Lead with scope, ownership and production results instead (S22 "lead with senior behaviors, not tenure alone"; S13 "We don't care how many years"; S14 years are a soft filter for early-career hiring).
- **Make projects and OSS carry weight:** production-grade side projects, evals, MCP servers and agents count (S13 "side projects count", S1, S19).
- **Be truthful if asked.** Keep commercial and independent experience clearly separated, as E6 does. Never inflate dates, titles or numbers (S2, S4, S33 «не преувеличивай»).
- **Apply anyway** when the stack and responsibilities match about 70% or more. Requirements are wish lists (S2 "best fit, not perfect"; S14).

**4.8 Russian vs English market tone.**
- **EN (international remote):**
  - Concise, direct, result-first, no adjectives about yourself.
  - No photo, age or nationality (S31, S12).
  - Add location with timezone and "Remote" (E8), plus English level (R7).
  - Informal for startups ("Hi [Name]") (S12).
  - First person in letters, no pronouns in CV bullets (S11).
  - Single-column PDF, ATS-safe (S24).
- **RU (hh.kz / hh.ru / Telegram):**
  - Telegram is often the first-contact channel; include the nick in the header (S33).
  - Business-friendly («Здравствуйте, [Имя]!» / «Добрый день»). Use «Вы» in emails and «вы» in chats.
  - Perfective past verbs without «я» in bullets («Разработал», «Сократил»).
  - The hh.ru profile usually has a photo and «Желаемая зарплата». For a PDF sent to international or remote roles, drop the photo (S32 vs S31). If salary comes up, specify gross vs net (S36).
  - Russian recruiters react strongly to adjective lists («ответственный, стрессоустойчивый…»). Replace them with one concrete story or fact (S32, S33, S35).
  - English loanwords for tech (RAG, p95, latency) are normal. Keep numbers in Russian notation (decimal comma: «2,4 с»), and the abbreviation «тыс.».
- **Both markets:**
  - Two separate documents, not a machine translation. Some Habr authors recommend an English CV plus a CV in the company's language (1b).
  - Keep the same facts and numbers in both versions (S12: discrepancies make recruiters suspicious).

---

