# Each channel may declare these fields:
#   max_chars (required): hard ceiling on body length. Over-length drafts
#       trigger a one-shot "shorten" retry.
#   min_chars (optional): floor on body length. Under-length drafts trigger
#       a one-shot "expand" retry. Length is measured AFTER stripping
#       attachment markup (banners, badges, CTA blocks, image/video lines,
#       hosted-image attachment markers, markdown image syntax) so the
#       limits reflect actual prose, not embedded media. Inline markdown
#       links count as their visible text only. Leave unset to skip the
#       floor check (current default for most channels).
CHANNEL_CONFIGS = {
    "twitter": {
        "display_name": "X / Twitter",
        "description": "Short-form social media post paired with a Chartmetric data visual",
        "max_chars": 600,
        "tone": "Punchy, relatable, scroll-stopping. Write short lines with breathing room between them. Mix up your openers: sometimes a sharp pain point ('70% of your week is manual research'), sometimes a tension or contrast ('Your data is everywhere... but your strategy shouldn't be'), sometimes a direct question ('Are you checking where your listeners are actually based?'), sometimes a bold capability statement. Keep each line short and punchy. Never make unverified claims about what artists or the industry are doing.",
        "format_rules": "Use line breaks between short punchy thoughts (2-4 lines, not a single run-on paragraph). Vary the opening style naturally. The goal is awareness of what Chartmetric makes possible, creating desire to try it. NEVER claim users are already doing something unless we have data proving it. Include 2-4 relevant hashtags at the end. No links in tweet body (added separately). Max 1-2 emoji if natural. STRICT: must be under 600 characters total including hashtags and line breaks.",
        "audience": "Music industry professionals, artists, managers, data analysts, A&R, and music fans who follow data trends on X/Twitter",
        "example_output_format": "[Punchy opening line]\n\n[Value prop or detail]\n\n#Hashtag1 #Hashtag2 #Hashtag3",
        "enabled": True,
    },
    "email_newsletter": {
        "display_name": "Marketing Newsletter",
        "description": "Product update block for the Chartmetric newsletter",
        "max_chars": 350,
        "tone": "Benefit-first, empowering, direct. Lead with what the user can NOW do, not what was built. Second person ('you'). Warm but concise.",
        "format_rules": "Structure (in this exact order): (1) **Title** on line 1 — bolded, ~5-7 word noun phrase that names the capability, NEVER the raw Internal Feature Name verbatim. (2) Subtitle on line 2 directly under the title (no blank line between them) — plain text (no bold, no italic), one short line that reframes the title from the user's perspective: what they can now do or find. Subtitle must NOT be a definition or rephrase of the title; it must shift to user benefit. (3) Blank line. (4) MAXIMUM 3 SHORT sentences total (including the CTA sentence) — first sentence leads with user benefit (start with a verb or 'you'), second is one key detail, third is the CTA like 'Check it out on any Artist Page.' No HTML tags. Assume a screenshot will follow the text block. Do NOT include subject lines — this is a section within a larger newsletter. STRICT RULE: never exceed 3 sentences after the title+subtitle pair.",
        "audience": "Existing Chartmetric users — artists, managers, labels, publishers who use the platform daily and scan newsletters quickly",
        "example_output_format": "**[Title: 5-7 word capability name]**\n[Subtitle: one-line user-benefit reframe, plain text]\n\n[1 benefit sentence]. [1 detail sentence]. [CTA sentence].",
        "enabled": True,
    },
    "email_standalone": {
        "display_name": "Email Standalone",
        "description": "Standalone product update email",
        "max_chars": 1500,
        "min_chars": 700,
        "tone": "Professional but warm. Benefit-led, second person. Room to paint the picture of the problem solved and highlight key use cases.",
        "format_rules": "Structure (in this exact order): (1) **Title** on line 1 — bolded, ~5-7 word noun phrase that names the capability. (2) Subtitle on line 2 directly under the title (no blank line between them) — plain text, one short line that reframes the title from the user's perspective: what they can now do or find. Subtitle must NOT be a definition or rephrase of the title; it must shift to user benefit. (3) Blank line. (4) 2-4 paragraphs with optional bullet points: first paragraph hooks with the user's pain point or aspiration; remaining paragraphs cover feature highlights, use cases, and CTA. No HTML. DO NOT include a 'Subject:' line — the subject is managed separately at send time.",
        "audience": "Chartmetric users, trial users, and prospects",
        "example_output_format": "**[Title: 5-7 word capability name]**\n[Subtitle: one-line user-benefit reframe, plain text]\n\n[Pain point hook paragraph]\n\n[Feature highlights]\n\n[CTA paragraph]",
        "enabled": True,
    },
    "email_standalone_digest": {
        "display_name": "Email Standalone (Digest Section)",
        "description": "Tight ~50-word section for a multi-feature standalone email digest",
        "max_chars": 350,
        "min_chars": 200,
        "tone": "Professional but warm. Benefit-led, second person. Tight and scannable — every word earns its place.",
        "format_rules": (
            "This is ONE SECTION inside a digest email that bundles several features together. "
            "Aim for ~50 words / ~300 characters total. Hard cap 350 characters.\n\n"
            "Structure (in this exact order):\n"
            "1. **Title** on line 1 — bolded (no '#' prefix), ~5-7 word noun phrase that names the capability. NEVER reuse the raw Internal Feature Name verbatim.\n"
            "2. Subtitle on line 2 directly under the title (no blank line between them) — plain text (no bold), one short line that reframes the title from the user's perspective (what they can now do or find). Subtitle must NOT be a definition or rephrase of the title.\n"
            "3. A blank line.\n"
            "4. 2-3 short sentences (~40 words) explaining what's new and the user benefit. Lead with value.\n"
            "5. (Optional) 1-2 single-line bullets starting with '- ' for concrete capabilities.\n"
            "6. (Optional) A single inline CTA as a markdown link like '[Try it now](https://...)' on its own line — only if the feature has a URL.\n\n"
            "Do NOT include a section-label chip, banner, or '## ' header — those are added by the renderer. "
            "Do NOT include a 'Subject:' line. No HTML. No greetings. No sign-offs."
        ),
        "audience": "Chartmetric users reading a monthly product digest email — busy, scanning on mobile",
        "example_output_format": "**[Title: 5-7 word capability name]**\n[Subtitle: one-line user-benefit reframe, plain text]\n\n[2-3 sentence value summary]. [Optional bullet list].\n\n[Optional CTA link]",
        "enabled": True,
    },
    "email_short": {
        "display_name": "Email Short",
        "description": "Quick, concise product update email — to the point",
        "max_chars": 500,
        "min_chars": 250,
        "tone": "Direct, benefit-first, concise. Get to the point fast. Second person ('you'). Warm but brief — every word must earn its place.",
        "format_rules": "Structure (in this exact order): (1) **Title** on line 1 — bolded, ~5-7 word noun phrase that names the capability. (2) Subtitle on line 2 directly under the title (no blank line between them) — plain text, one short line that reframes the title from the user's perspective: what they can now do or find. Subtitle must NOT be a definition or rephrase of the title. (3) Blank line. (4) 1-2 very short paragraphs (2-3 sentences total): first paragraph is what's new and why it matters; optional second paragraph is a single CTA sentence. No bullet points. No HTML. Keep it scannable on mobile — this is a quick update, not a deep dive. DO NOT include a 'Subject:' line — the subject is managed separately at send time.",
        "audience": "Chartmetric users and trial users — busy professionals who scan emails quickly on mobile",
        "example_output_format": "**[Title: 5-7 word capability name]**\n[Subtitle: one-line user-benefit reframe, plain text]\n\n[1-2 sentence benefit hook]. [CTA sentence].",
        "enabled": True,
    },
    "email_medium": {
        "display_name": "Email Medium",
        "description": "Feature update email with key use cases and benefits",
        "max_chars": 1000,
        "min_chars": 600,
        "tone": "Professional but warm. Benefit-led, second person. Room to paint the picture of the problem solved and highlight key use cases.",
        "format_rules": "Structure (in this exact order): (1) **Title** on line 1 — bolded, ~5-7 word noun phrase that names the capability. (2) Subtitle on line 2 directly under the title (no blank line between them) — plain text, one short line that reframes the title from the user's perspective: what they can now do or find. Subtitle must NOT be a definition or rephrase of the title. (3) Blank line. (4) 2-3 short paragraphs with optional bullet points: first paragraph hooks with the user's pain point or aspiration; second uses 2-4 bullet points to highlight key benefits or use cases; third is a CTA with a specific next step. Keep paragraphs to 2-3 sentences each. No HTML. DO NOT include a 'Subject:' line — the subject is managed separately at send time.",
        "audience": "Chartmetric users and trial users — may not be daily active, so context-set briefly",
        "example_output_format": "**[Title: 5-7 word capability name]**\n[Subtitle: one-line user-benefit reframe, plain text]\n\n[Pain point hook paragraph]\n\n[Feature highlights with bullet points]\n\n[CTA paragraph]",
        "enabled": True,
    },
    "email_long": {
        "display_name": "Email Long",
        "description": "Comprehensive feature breakdown email with full detail",
        "max_chars": 1500,
        "min_chars": 900,
        "tone": "Thorough, informative, empowering. Second person. Take the reader on a journey from problem to solution to action. Authoritative but accessible.",
        "format_rules": "Structure (in this exact order): (1) **Title** on line 1 — bolded, ~5-7 word noun phrase that names the capability. (2) Subtitle on line 2 directly under the title (no blank line between them) — plain text, one short line that reframes the title from the user's perspective: what they can now do or find. Subtitle must NOT be a definition or rephrase of the title. (3) Blank line. (4) 3-5 paragraphs with bullet points and detail: first paragraph hooks with pain point or industry context; second introduces the feature and its core benefit; third is a detailed breakdown with 3-5 bullet points covering specific capabilities, who benefits, and how; fourth is a real-world scenario or use case; fifth is a strong CTA. No HTML. DO NOT include a 'Subject:' line — the subject is managed separately at send time.",
        "audience": "Chartmetric users, trial users, and prospects who want to understand the full scope of what's new",
        "example_output_format": "**[Title: 5-7 word capability name]**\n[Subtitle: one-line user-benefit reframe, plain text]\n\n[Context/pain point paragraph]\n\n[Feature introduction paragraph]\n\n[Detailed breakdown with bullets]\n\n[Use case or scenario paragraph]\n\n[CTA paragraph]",
        "enabled": True,
    },
    "inapp": {
        "display_name": "In-App Announcement",
        "description": "Product announcement displayed inside the Chartmetric app",
        "max_chars": 800,
        "tone": "Clear, benefit-focused, user-empowering. Like a helpful product guide \u2014 informative but not pushy. Address the user directly.",
        "format_rules": "Structure (in this exact order): (1) **Title** on line 1 — bolded, ~5-7 word noun phrase that names the capability, with ! at the end for energy. (2) Subtitle on line 2 directly under the title (no blank line between them) — plain text (no bold, no '!'), one short line that reframes the title from the user's perspective: what they can now do or find. Subtitle must NOT be a definition or rephrase of the title. (3) Blank line. (4) 1-2 sentences introducing the feature through the lens of what the user can now do. (5) Blank line, then 2-3 bullet points with specific capabilities or highlights (each bullet 1-2 sentences). (6) End with a clear navigational CTA telling the user exactly where to find it (e.g., 'Head to any Track Page, click the Playlist tab, and start discovering...'). Assume a screenshot will be shown below the text. No emoji in body text.",
        "audience": "Active Chartmetric users seeing this inside the product \u2014 they're already logged in and working",
        "example_output_format": "**[Title: 5-7 word capability name]!**\n[Subtitle: one-line user-benefit reframe, plain text]\n\n[1-2 intro sentences]\n\n\u2022 [Capability 1]\n\u2022 [Capability 2]\n\n[Navigational CTA]",
        "enabled": True,
    },
    "linkedin": {
        "display_name": "LinkedIn Post",
        "description": "Professional thought-leadership post connecting industry trends to Chartmetric data or features",
        "max_chars": 2000,
        "tone": "Insight-led, data-backed, industry-expert voice. Open with a trend or observation that hooks music industry professionals, then weave in Chartmetric data as the proof point. Authoritative but not salesy \u2014 you're sharing knowledge, not selling a product.",
        "format_rules": "Structure: Hook opening line (industry trend, surprising data point, or provocative observation \u2014 use a single emoji like \U0001f3b5 if natural), blank line, 2-3 short paragraphs that build the narrative from trend \u2192 data \u2192 insight. Reference specific artists, numbers, or playlists when relevant. End with a CTA linking to a full article or inviting discussion. Include 3-5 hashtags on the final line. Use line breaks between paragraphs for readability. 150-300 words.",
        "audience": "Music industry executives, label heads, A&R professionals, playlist curators, and tech leaders on LinkedIn",
        "example_output_format": "[Trend hook line with optional emoji]\n\n[Paragraph expanding on the trend with specifics]\n\n[Paragraph connecting to data/Chartmetric insight]\n\n[CTA to article or discussion prompt]\n\n#hashtag1 #hashtag2 #hashtag3",
        "enabled": True,
    },
    "notion_monthly": {
        "display_name": "Notion Monthly Meeting Doc",
        "description": "Feature summary block for monthly product meeting notes",
        "max_chars": 600,
        "tone": "Concise, factual, scannable. Like a quick update in a meeting doc. Get to the point in 2-3 sentences.",
        "format_rules": "Structure (in this exact order): (1) ### **Title** on line 1 — bolded H3 heading, ~5-7 word noun phrase that names the capability. NEVER reuse the raw Internal Feature Name verbatim. (2) Subtitle on line 2 directly under the title (no blank line between them) — plain text (no bold, no '#'), one short line that reframes the title from the user's perspective: what they can now do or find. Subtitle must NOT be a definition or rephrase of the title. (3) Blank line. (4) 2-3 sentences covering what changed and why it matters to users. Include a relevant link if available. (5) End with key contributors (first names only, e.g. 'Hanby' not 'Hanby Choi'). That's it. No separate sections for Overview, User Impact, Technical Notes. Keep it tight and scannable.",
        "audience": "Chartmetric leadership and cross-functional team in a monthly product review meeting",
        "example_output_format": "### **[Title: 5-7 word capability name]**\n[Subtitle: one-line user-benefit reframe, plain text]\n\n[2-3 sentence summary of what changed and user impact]. [Link if relevant]\n\nKey Contributors: [first names only]",
        "enabled": True,
    },
    "article_hmc": {
        "display_name": "HMC Blog Article",
        "description": "SEO-optimized long-form article for How Music Charts (hmc.chartmetric.com)",
        "max_chars": 10000,
        "tone": "Strategic, empowering, direct-address ('you'). Write like a savvy music industry mentor coaching an ambitious indie artist or professional. Conversational authority \u2014 confident and opinionated but backed by data and real examples. Not dry or academic. Not promotional \u2014 Chartmetric features are framed as tools the reader can use, not products being sold.",
        "format_rules": "Structure: (1) `meta_description:` line (155 chars max) for SEO. (2) Blank line. (3) `# Title` H1 — SEO-friendly with the primary keyword, ~5-7 word noun phrase that names the capability or strategy. (4) Subtitle on the next line directly under the H1 (no blank line between them) — plain text (no '#', no bold), one short line that reframes the title from the reader's perspective: what they can now do or find. Subtitle must NOT be a definition or rephrase of the title. (5) Blank line. (6) Intro paragraph: hook with the reader's challenge or aspiration, cite a compelling stat to establish stakes, preview what the article covers. Target long-tail SEO phrases naturally (e.g., 'how do I get my song on Spotify playlists'). (7) Then 3-5 H2 sections, each covering one Chartmetric feature or strategy. Each H2 section should include: feature name + where to find it, what it does framed as user benefit, step-by-step guidance, a 'Why this matters' block with data/context, and a 'Pro tip' with insider advice. Use specific numbers and real-world examples (artist names, percentage growth, stream counts). End with a short empowering closing section. Write in second person ('you'). 1000-2000 words.",
        "audience": "Independent artists, emerging managers, music students, and early-career professionals searching Google for music promotion strategies and data tools",
        "example_output_format": "meta_description: [155-char SEO description]\n\n# [Title: SEO-friendly 5-7 word capability/strategy name]\n[Subtitle: one-line user-benefit reframe, plain text]\n\n[Hook intro paragraph with stats + SEO phrases]\n\n## [Feature/Strategy 1 Name]\n[Feature description + user benefit]\n[How to access/use it]\n**Why this matters:** [data-backed context]\n**Pro tip:** [insider advice]\n\n## [Feature/Strategy 2 Name]\n...\n\n## [Empowering Closing Header]\n[1-2 sentence empowering close]",
        "enabled": True,
    },
    "did_you_know": {
        "display_name": "\U0001f4a1 Did You Know?",
        "description": "Fun educational discovery content about hidden or underused Chartmetric features",
        "max_chars": 280,
        "tone": "Friendly, surprising, educational. Like a knowledgeable colleague sharing a cool hidden trick. Conversational and upbeat \u2014 make the reader feel like they just discovered a secret superpower.",
        "format_rules": "Structure: Start with 'Did you know?' followed by a surprising or lesser-known fact about the Chartmetric feature. Then include a clear, actionable CTA starting with 'Try it:' that tells the user exactly how to access or use the feature. Keep it concise and punchy \u2014 this is a single-tweet-length tip. No hashtags, no links, no emoji in body. Must be under 280 characters total.",
        "audience": "Existing Chartmetric users who may not be aware of all platform capabilities \u2014 power users and casual users alike",
        "example_output_format": "Did you know? [surprising fact about the feature]. Try it: [specific actionable CTA].",
        "enabled": True,
    },
}
