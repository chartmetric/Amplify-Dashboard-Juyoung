-- ============================================================
-- Announcement Post Migration: Frill -> Internal DB
-- Generated from frillMockResponses.ts
-- ============================================================

BEGIN;

-- 1. Create tables
-- ============================================================

CREATE TABLE IF NOT EXISTS announcement_post (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    content         JSONB NOT NULL,
    image_url       TEXT,
    is_published    BOOLEAN NOT NULL DEFAULT false,
    is_pinned       BOOLEAN NOT NULL DEFAULT false,
    is_boosted      BOOLEAN NOT NULL DEFAULT false,
    published_at    TIMESTAMP WITHOUT TIME ZONE,
    created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    modified_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_announcement_post_published
    ON announcement_post (is_published, is_pinned DESC, published_at DESC);

CREATE TABLE IF NOT EXISTS announcement_category (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    color           TEXT NOT NULL,
    created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    modified_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS l_announcement_post_category (
    announcement_post_id     INT NOT NULL REFERENCES announcement_post(id) ON DELETE CASCADE,
    announcement_category_id INT NOT NULL REFERENCES announcement_category(id) ON DELETE CASCADE,
    PRIMARY KEY (announcement_post_id, announcement_category_id)
);

-- 2. Add modified_at triggers
-- ============================================================

CREATE TRIGGER update_announcement_post_modified_time
    BEFORE UPDATE ON announcement_post
    FOR EACH ROW
    EXECUTE FUNCTION chartmetric.update_modified_column();

CREATE TRIGGER update_announcement_category_modified_time
    BEFORE UPDATE ON announcement_category
    FOR EACH ROW
    EXECUTE FUNCTION chartmetric.update_modified_column();

-- 3. Add FK constraints to existing tables
-- ============================================================

ALTER TABLE announcement_reaction
    ADD CONSTRAINT announcement_reaction_announcement_post_fk
    FOREIGN KEY (announcement_id) REFERENCES announcement_post(id) ON DELETE CASCADE;

ALTER TABLE announcement_comment
    ADD CONSTRAINT announcement_comment_announcement_post_fk
    FOREIGN KEY (announcement_id) REFERENCES announcement_post(id) ON DELETE CASCADE;

-- 4. Seed categories
-- ============================================================

INSERT INTO announcement_category (id, name, color) VALUES
    (1, 'New Feature', '#6392D9'),
    (2, 'Improvement', '#63C8D9'),
    (3, 'Announcement', '#FF3C3C');

-- 5. Seed announcement posts
-- ============================================================

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    1,
    'Find Your Perfect Playlist Match with Playlists to Pitch!',
    '[{"type":"announcement-title","children":[{"text":"Find Your Perfect Playlist Match with Playlists to Pitch!"}]},{"type":"paragraph","children":[{"text":"Looking for the perfect playlists to pitch your track? Our new "},{"text":"Playlists to Pitch","bold":true},{"text":" feature takes the guesswork out of playlist outreach by recommending playlists tailored specifically to your track."}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Each recommendation comes with a Fit Analysis explaining exactly why we think the playlist is a strong match - so you can pitch with confidence and never miss an opportunity to grow your track. "}]},{"type":"list-item","children":[{"text":"You''ll also see key metrics at a glance: Added Reach, Added Streams, and direct links to reach out to playlist curators."}]}]},{"type":"paragraph","children":[{"text":"Head to any Track Page, click the Playlist tab, and start discovering your next playlist placement today!"}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/1723541b-a543-414d-916f-009ac96f00e0.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/1723541b-a543-414d-916f-009ac96f00e0.png',
    true,
    true,
    false,
    '2026-01-26T07:07:57',
    '2026-01-26T07:06:26',
    '2026-02-24T21:09:42'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    2,
    'Introducing Enhanced Influencer Insights on Artist UGC Tab 🎉',
    '[{"type":"announcement-title","children":[{"text":"Introducing Enhanced Influencer Insights on Artist UGC Tab 🎉"}]},{"type":"paragraph","children":[{"text":"The Influencer Table on the Artist User Generated Contents tab now delivers richer data and better usability to help you identify the right influencers for your campaigns."}]},{"type":"paragraph","children":[{"text":"What''s new","bold":true}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"More context at a glance: See verified status, top tracks, hashtags, and audience metrics without leaving the table"}]}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Smarter filtering:  Narrow down influencers based on the criteria that matter most to your strategy"}]},{"type":"list-item","children":[{"text":"Clearer visualization: Improved layout makes it easier to scan and compare influencer profiles"}]}]},{"type":"paragraph","children":[{"text":"Explore the upgraded Influencer Table on any Artist UGC/TikTok tab and discover influencers driving real impact for your artists!"}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/f1803c87-0b6d-4783-9eb4-bb5d10ccc5fd.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/f1803c87-0b6d-4783-9eb4-bb5d10ccc5fd.png',
    true,
    false,
    false,
    '2026-01-13T16:05:36',
    '2026-01-13T15:59:11',
    '2026-02-24T21:09:47'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    3,
    'Introducing Our Brand New Navigation Bar',
    '[{"type":"announcement-title","children":[{"text":"Introducing Our Brand New Navigation Bar"}]},{"type":"paragraph","children":[{"text":"We''ve redesigned the navigation bar to be leaner, cleaner, and more intuitive than ever!"}]},{"type":"paragraph","children":[{"text":"What''s new:","bold":true}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Collapsible design: The nav bar now expands on hover, giving you more screen space when you need it"}]}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Organized sections: Items are grouped into categories like \"CATALOG\" and \"TOOLS\" for faster navigation"}]}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Built-in search: Find any nav bar item instantly with the new search bar at the top"}]}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Favorites feature: Mark your most-used items as favorites, and they''ll automatically move to a dedicated \"FAVORITES\" section for quick access"}]}]},{"type":"paragraph","children":[{"text":"Experience a smoother, more personalized workflow—check out the new nav bar today on "},{"type":"link","url":"https://app.chartmetric.com/artists","children":[{"text":"Chartmetric"}]},{"text":"!"}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/6b0d7233-76c5-4ae9-bfc0-afbd82999e0b.gif","alt":"chrome-capture-2025-11-24 (1)","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/6b0d7233-76c5-4ae9-bfc0-afbd82999e0b.gif',
    true,
    false,
    false,
    '2025-11-24T15:38:25',
    '2025-11-24T15:19:45',
    '2026-02-24T21:09:50'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    4,
    'Onesheet just got a major upgrade! ',
    '[{"type":"announcement-title","children":[{"text":"Onesheet just got a major upgrade! ","bold":true}]},{"type":"paragraph","children":[{"text":"Onesheet just got a major upgrade! ","bold":true}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"A new, cleaner, and faster editing flow, giving you more flexibility to customize your artist sheets."}]}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"New advanced editing features: embed images directly in text and quickly navigate to any block''s editing menu with a single click."}]}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Faster load times across the app for a smoother workflow."}]}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"A refreshed dashboard and settings page to keep your sheets and preferences organized"}]}]},{"type":"paragraph","children":[{"text":"Try it out today "},{"type":"link","url":"https://www.onesheet.club/","children":[{"text":"here"}]},{"text":" and let us know what you think!"}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/3835bfcf-9c6e-4139-a49c-eda2b917be6b.png","alt":"unnamed","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/3835bfcf-9c6e-4139-a49c-eda2b917be6b.png',
    true,
    false,
    false,
    '2025-10-29T20:04:51',
    '2025-10-03T01:32:04',
    '2026-02-24T21:09:52'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    5,
    'New Snapchat Data Now Available!',
    '[{"type":"announcement-title","children":[{"text":"New Snapchat Data Now Available!"}]},{"type":"paragraph","children":[{"text":"You can now track Snapchat data for artists!","bold":true}]},{"type":"paragraph","children":[{"text":"Dive deeper into your artist''s social presence with comprehensive Snapchat data now integrated into Chartmetric."}]},{"type":"paragraph","children":[{"text":"Track follower growth and analyze audience demographics directly from your artist profiles and lists, giving you a complete picture of social media performance across another key platform where music discovery happens."}]},{"type":"paragraph","children":[{"text":"Ready to explore? Check out the Snapchat data for your artist today!"}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/58e6f9f8-fbdc-4846-8954-0fe40a56d94e.png","alt":"image","children":[{"text":""}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/f5c9baf7-7b8c-45ec-b008-3a0cf6083a17.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/58e6f9f8-fbdc-4846-8954-0fe40a56d94e.png',
    true,
    false,
    false,
    '2025-10-16T18:43:03',
    '2025-10-02T02:11:18',
    '2025-11-24T23:30:07'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    6,
    'Introducing the Reports Center: All Your Reports in One Place',
    '[{"type":"announcement-title","children":[{"text":"Introducing the Reports Center: All Your Reports in One Place"}]},{"type":"paragraph","children":[{"text":"Easily manage reports for your artists, tracks, and albums—all in one place with Chartmetric''s new Reports Center Homepage."}]},{"type":"paragraph","children":[{"text":"Here''s a glimpse of what you can do:"}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Never miss key growth with Custom Alerts"}]},{"type":"list-item","children":[{"text":"Spot industry trends instantly through Noteworthy Insights"}]},{"type":"list-item","children":[{"text":"Track playlist adds and removals with Playlist Monitoring"}]},{"type":"list-item","children":[{"text":"Filter and search all reports easily "}]}]},{"type":"paragraph","children":[{"text":"Check it out "},{"type":"link","url":"https://app.chartmetric.com/reports","children":[{"text":"here","bold":true}]},{"text":" and never miss a beat!"}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/e53ed020-3f99-4fae-95ad-595eaab6a671.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/e53ed020-3f99-4fae-95ad-595eaab6a671.png',
    true,
    false,
    false,
    '2025-10-16T18:41:40',
    '2025-10-02T02:28:18',
    '2025-10-29T19:47:02'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    7,
    'Introducing New Smart Filters on Artist List',
    '[{"type":"announcement-title","children":[{"text":"Introducing New Smart Filters on Artist List"}]},{"type":"paragraph","children":[{"text":"Our new Smart Filters make finding artists faster and easier than ever, with an upgraded design that cuts down on endless clicking."}]},{"type":"paragraph","children":[{"text":"Now you can:"}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Filter with fewer clicks — streamlined controls put the power at your fingertips"}]}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Find artists faster — intuitive design helps you zero in on the stats that matter"}]}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"Work more efficiently — spend less time searching, more time strategizing"}]}]},{"type":"paragraph","children":[{"text":"Head to the "},{"type":"link","url":"https://app.chartmetric.com/artists","children":[{"text":"Artist List"}]},{"text":" now and experience the upgrade for yourself!"}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/b71ee0cd-a359-452d-8511-36bb53ff727a.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/b71ee0cd-a359-452d-8511-36bb53ff727a.png',
    true,
    false,
    false,
    '2025-10-16T18:41:25',
    '2025-10-13T19:11:47',
    '2026-03-17T01:31:52'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    8,
    'Discover Shortlist-level Insights on the New Shortlist Page!',
    '[{"type":"announcement-title","children":[{"text":"Discover Shortlist-level Insights on the New Shortlist Page!"}]},{"type":"paragraph","children":[{"text":"Ever wondered what valuable insights lie across your shortlists?"}]},{"type":"paragraph","children":[{"text":"With Chartmetric''s new "},{"text":"Shortlist Page","bold":true},{"text":", you can now:"}]},{"type":"bulleted-list","children":[{"type":"list-item","children":[{"text":"View audience demographics, DSP performance, genre/country distributions across your shortlist"}]},{"type":"list-item","children":[{"text":"Compare entities within the shortlist across key metrics"}]},{"type":"list-item","children":[{"text":"Share and manage shortlists effortlessly with your team"}]}]},{"type":"paragraph","children":[{"text":"👉 Head to your Shortlists in the top left View All menu or start exploring with "},{"type":"link","url":"https://app.chartmetric.com/shortlist/174703","children":[{"text":"Coachella 2025 Shortlist"}]},{"text":"!"}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/bd16b217-1795-44f6-8b78-ffad8239eca3.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/bd16b217-1795-44f6-8b78-ffad8239eca3.png',
    true,
    false,
    false,
    '2025-10-02T02:32:43',
    '2025-10-02T02:32:08',
    '2025-11-25T23:22:00'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    9,
    'Artist Profiles Just Got a Major Glow-Up!',
    '[{"type":"announcement-title","children":[{"text":"Artist Profiles Just Got a Major Glow-Up!"}]},{"type":"paragraph","children":[{"text":"Your favorite artists now have stunning new profile banners that are as unique as their music! ","bold":true}]},{"type":"paragraph","children":[{"text":"Artist, track, and playlist pages all now feature a "},{"text":"beautifully designed header","bold":true},{"text":" using their own imagery as the background, creating a more unique and immersive experience."}]},{"type":"paragraph","children":[{"text":"Plus, we''ve made it easier than ever to access "},{"text":"Onesheet","bold":true},{"text":" directly from artist profiles, so you can quickly create a dynamic showcase for your artist right from the page."}]},{"type":"paragraph","children":[{"text":"Check out the refreshed artist profiles today and see the difference for yourself."}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/94441179-16fa-4a34-b58e-0377c9ca1276.png","alt":"image","children":[{"text":""}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/1191743e-4adc-4636-be42-0ab79e3322aa.png","alt":"image","children":[{"text":""}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/c7af730d-2fa6-40eb-91e4-0bbdb7bb374d.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/94441179-16fa-4a34-b58e-0377c9ca1276.png',
    true,
    false,
    false,
    '2025-10-02T02:32:00',
    '2025-10-02T02:30:50',
    '2025-10-02T02:32:00'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    10,
    'Monitor Artist''s Stats on the Go with Mobile Widgets!',
    '[{"type":"announcement-title","children":[{"text":"Monitor Artist''s Stats on the Go with Mobile Widgets!"}]},{"type":"paragraph","children":[{"text":"With the Chartmetric Mobile Widgets, your artist''s key stats are just a glance away on your home screen."}]},{"type":"paragraph","children":[{"text":"You can stay instantly updated with artists'' latest stats and changes. "}]},{"type":"paragraph","children":[{"text":"Plus, you''re in control: customize which artists and metrics you want to track, and pick from multiple widget views and sizes to match your style."}]},{"type":"paragraph","children":[{"text":"Now available on "},{"type":"link","url":"https://apps.apple.com/app/chartmetric/id1522918776","children":[{"text":"iOS"}]},{"text":" and "},{"type":"link","url":"https://play.google.com/store/apps/details?id=com.chartmetric","children":[{"text":"Android"}]},{"text":" — download your Chartmetric app and start using widgets today!"}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/987c1c9c-7a97-44fd-9daf-bc261d27e242.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/987c1c9c-7a97-44fd-9daf-bc261d27e242.png',
    true,
    false,
    false,
    '2025-10-02T02:30:34',
    '2025-10-02T02:29:41',
    '2025-10-02T02:30:44'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    11,
    'New User-Generated Content (UGC) tab on Artist Page',
    '[{"type":"announcement-title","children":[{"text":"New User-Generated Content (UGC) tab on Artist Page"}]},{"type":"paragraph","children":[{"text":"Discover how fans are engaging with your music in the new User Generated Content tab on Artist Pages!"}]},{"type":"paragraph","children":[{"text":"Monitor comment sentiment, engagement analytics, and see where users are posting videos featuring your artist''s tracks. This powerful tool helps you identify which platforms are driving the most engagement and where fans are most actively creating content with your artist''s music."}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/6988f594-4353-4b34-96c5-a30634685f9d.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/6988f594-4353-4b34-96c5-a30634685f9d.png',
    true,
    false,
    false,
    '2025-10-02T02:28:00',
    '2025-10-02T02:27:23',
    '2025-10-02T02:28:00'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    12,
    'Discover Features in the New Chartmetric Onboarding Center!',
    '[{"type":"announcement-title","children":[{"text":"Discover Features in the New Chartmetric Onboarding Center!"}]},{"type":"paragraph","children":[{"text":"Looking to enhance your Chartmetric expertise? Our new Onboarding Center is now available! Simply click the book icon in the top right corner next to your profile to access streamlined demonstrations of our features."}]},{"type":"paragraph","children":[{"text":"The Onboarding Center offers simplified views designed to help you explore and master Chartmetric without feeling overwhelmed. Start your learning journey today!"}]},{"type":"paragraph","children":[{"text":"Tip: You can also now update your name and profile picture in your account settings!","italic":true}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/6deac96e-514e-4cc0-b228-19df2ab2b118.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/6deac96e-514e-4cc0-b228-19df2ab2b118.png',
    true,
    false,
    false,
    '2025-10-02T02:26:53',
    '2025-10-02T02:24:48',
    '2025-10-02T02:26:53'
);

INSERT INTO announcement_post (id, title, content, image_url, is_published, is_pinned, is_boosted, published_at, created_at, modified_at) VALUES (
    13,
    'Chartmetric Teams: Share & Collaborate',
    '[{"type":"announcement-title","children":[{"text":"Chartmetric Teams: Share & Collaborate"}]},{"type":"paragraph","children":[{"text":"We''re thrilled to announce that premium users can now create dedicated "},{"text":"teams","bold":true},{"text":" to streamline collaboration. "},{"text":"Teams","bold":true},{"text":" lets you organize sub-groups of users and easily share Chartmetric content with exactly who needs it."}]},{"type":"paragraph","children":[{"text":"Getting Started","bold":true}]},{"type":"numbered-list","children":[{"type":"list-item","children":[{"text":"Click \"Create your first team\" or the \"+\" icon from your profile dropdown in the top-right corner of your screen"}]},{"type":"list-item","children":[{"text":"Name your team and add other premium users"}]},{"type":"list-item","children":[{"text":"Manage settings anytime through the dropdown menu—set icons, rename teams, or adjust member permissions"}]},{"type":"list-item","children":[{"text":"Create multiple teams to suit your needs by clicking the \"+\" icon "}]},{"type":"list-item","children":[{"text":"Teams make sharing shortlists effortless today, with reports and additional content sharing coming soon. Ready to enhance your workflow? Create your first team now and experience a new level of collaborative efficiency with Chartmetric."}]}]},{"type":"image","url":"https://frill-prod.s3.us-west-2.amazonaws.com/2958395/b7561378-c8bb-4952-a98d-5c0846b8d5da.png","alt":"image","children":[{"text":""}]}]'::JSONB,
    'https://frill-prod.s3.us-west-2.amazonaws.com/2958395/b7561378-c8bb-4952-a98d-5c0846b8d5da.png',
    true,
    false,
    false,
    '2025-10-02T02:23:52',
    '2025-10-02T02:22:43',
    '2025-10-02T02:23:52'
);

-- Reset sequence to next available ID
SELECT setval('announcement_post_id_seq', (SELECT MAX(id) FROM announcement_post));

-- 6. Seed category mappings
-- ============================================================

INSERT INTO l_announcement_post_category (announcement_post_id, announcement_category_id) VALUES
    (1, 1),
    (1, 3),
    (2, 2),
    (2, 3),
    (3, 3),
    (3, 2),
    (4, 2),
    (4, 3),
    (5, 1),
    (5, 3),
    (6, 1),
    (6, 3),
    (7, 2),
    (7, 3),
    (8, 1),
    (8, 3),
    (9, 3),
    (9, 2),
    (10, 1),
    (10, 3),
    (11, 1),
    (11, 3),
    (13, 1),
    (13, 3);

COMMIT;
