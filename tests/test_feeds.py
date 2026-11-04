import unittest
from pathlib import Path

from omapod.feeds import chapters, opml, transcripts
from omapod.feeds.parser import NotAFeedError, parse_date, parse_duration, parse_feed

FIX = Path(__file__).with_name("fixtures")


class ParserTest(unittest.TestCase):
    def test_rss(self):
        feed = parse_feed((FIX / "feed_rss.xml").read_bytes(), "https://example.org/feed.xml")
        self.assertEqual(feed.title, "Test Cast")
        self.assertEqual(feed.author, "Jane Doe")
        self.assertEqual(feed.image_url, "https://example.org/cover.jpg")
        self.assertEqual(feed.next_page_url, "https://example.org/feed?page=2")
        self.assertEqual(feed.funding, [{"url": "https://example.org/donate", "title": "Support us"}])
        self.assertEqual(feed.podcast_guid, "c2f2f2f2-0000-4000-8000-000000000001")
        self.assertEqual(len(feed.items), 4)
        ep3 = feed.items[0]
        self.assertEqual(ep3.guid, "ep-3")
        self.assertEqual(ep3.duration, 3723000)
        self.assertEqual(ep3.media_size, 12345678)
        self.assertIn("show notes", ep3.description)
        self.assertEqual(ep3.image_url, "https://example.org/ep3.jpg")
        self.assertEqual((ep3.season, ep3.episode_number), (2, 3))
        self.assertEqual(ep3.chapters_url, "https://example.org/ep3.json")
        self.assertEqual(ep3.transcript_type, "application/json")
        self.assertEqual([c["start"] for c in ep3.psc_chapters], [0, 300000])
        ep2 = feed.items[1]
        self.assertEqual(ep2.title, "Episode 2 & friends")
        self.assertEqual(ep2.media_size, 0)  # AntennaPod ignores lengths below 16 KiB
        self.assertEqual(ep2.duration, 2723000)
        ep1 = feed.items[2]
        self.assertEqual(ep1.guid, "")
        self.assertEqual(ep1.media_type, "audio/mpeg")
        self.assertEqual(ep1.duration, 2710000)
        post = feed.items[3]
        self.assertEqual(post.media_url, "")

    def test_broken_feed_is_repaired(self):
        feed = parse_feed((FIX / "feed_broken.xml").read_bytes())
        self.assertIn("Broken", feed.title)
        self.assertEqual(feed.author, "Undeclared Prefix")
        self.assertEqual(feed.items[0].title, "Café episode")
        self.assertEqual(feed.items[0].media_url, "https://example.org/b1.mp3?a=1&b=2")
        self.assertEqual(feed.items[0].duration, 90000)
        self.assertGreater(feed.items[0].pub_date, 0)

    def test_atom(self):
        feed = parse_feed((FIX / "feed_atom.xml").read_bytes())
        self.assertEqual(feed.title, "Atom Cast")
        self.assertEqual(feed.author, "Atom Author")
        self.assertEqual(feed.image_url, "https://atom.example/logo.png")
        item = feed.items[0]
        self.assertEqual(item.media_url, "https://atom.example/1.ogg")
        self.assertEqual(item.duration, 1800000)
        self.assertEqual(item.link, "https://atom.example/1")

    def test_html_page_lists_feeds(self):
        with self.assertRaises(NotAFeedError) as ctx:
            parse_feed((FIX / "page.html").read_bytes(), "https://site.example/podcast/")
        urls = [c["url"] for c in ctx.exception.candidates]
        self.assertEqual(urls, ["https://site.example/feed.xml", "https://site.example/atom.xml"])

    def test_dates(self):
        self.assertEqual(parse_date("Wed, 08 Oct 2026 08:00:00 GMT"), 1791446400)
        self.assertEqual(parse_date("2026-10-08T08:00:00Z"), 1791446400)
        self.assertEqual(parse_date("2026-10-08 10:00:00+02:00"), 1791446400)
        self.assertEqual(parse_date("Wed, 8 Oct 2026 08:00:00 GMT+0"), 1791446400)
        self.assertGreater(parse_date("8 Okt 2026 08:00:00 +0000"), 0)
        self.assertEqual(parse_date("garbage"), 0)

    def test_durations(self):
        self.assertEqual(parse_duration("1:02:03"), 3723000)
        self.assertEqual(parse_duration("62:03"), 3723000)
        self.assertEqual(parse_duration("3723"), 3723000)
        self.assertEqual(parse_duration("3723.5"), 3723500)
        self.assertEqual(parse_duration("1h 2m 3s"), 3723000)
        self.assertEqual(parse_duration(""), 0)


class OtherFormatsTest(unittest.TestCase):
    def test_opml_roundtrip(self):
        entries = opml.parse_opml((FIX / "subs.opml").read_bytes())
        self.assertEqual([e["url"] for e in entries],
                         ["https://example.org/feed", "https://atom.example/feed.xml"])
        from omapod.models import Feed

        text = opml.build_opml([Feed(url="https://a/feed", title="A & B", link="https://a")])
        self.assertEqual(opml.parse_opml(text)[0]["title"], "A & B")

    def test_json_chapters_and_merge(self):
        remote = chapters.parse_json_chapters((FIX / "chapters.json").read_text())
        self.assertEqual([c["start"] for c in remote], [0, 300500, 1200000])
        feed = [{"start": 0, "title": "Intro", "url": "", "image": ""},
                {"start": 300000, "title": "", "url": "x", "image": ""},
                {"start": 1200000, "title": "Outro", "url": "", "image": ""}]
        merged = chapters.merge(feed, remote)
        self.assertEqual(merged[1]["title"], "Topic")
        self.assertEqual(merged[1]["url"], "x")
        self.assertEqual(chapters.merge(None, remote), remote)
        self.assertEqual(chapters.merge(feed[:2], remote), remote)
        self.assertEqual(chapters.index_at(remote, 400000), 1)
        self.assertEqual(chapters.index_at([{"start": 1000}], 0), -1)

    def test_transcript(self):
        segments = transcripts.parse_transcript((FIX / "transcript.srt").read_text(), "application/x-subrip")
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].speaker, "Jane")
        self.assertEqual(segments[0].text, "Hello and welcome to the show.")
        self.assertEqual(segments[1].start, 5000)
        json_text = '{"segments":[{"speaker":"A","startTime":1.5,"endTime":2,"body":"Hi."}]}'
        self.assertEqual(transcripts.parse_transcript(json_text, "application/json")[0].start, 1500)
        html_text = "<cite>Bob:</cite><time>0:05</time><p>Yes.</p><cite>Ann:</cite><time>0:09</time><p>No.</p>"
        parsed = transcripts.parse_transcript(html_text, "text/html")
        self.assertEqual([s.speaker for s in parsed], ["Bob", "Ann"])


if __name__ == "__main__":
    unittest.main()
