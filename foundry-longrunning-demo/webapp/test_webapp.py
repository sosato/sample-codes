"""Unit tests for request validation and response citation extraction."""

from __future__ import annotations

import unittest
from datetime import date

from main import (
    TripRequest,
    _compact_event,
    build_trip_prompt,
    extract_citations,
    parse_trip_sections,
)


class DummyResponse:
    def model_dump(self, mode: str):
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "annotations": [
                                {
                                    "url": "https://example.com/hotel",
                                    "title": "Hotel",
                                }
                            ]
                        }
                    ],
                },
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [
                            {
                                "url": "https://example.com/flight",
                                "title": "Flight",
                            },
                            {
                                "url": "https://example.com/hotel",
                                "title": "Duplicate",
                            },
                        ]
                    },
                },
            ]
        }


class WebAppTests(unittest.TestCase):
    def test_private_family_trip_prompt(self):
        request = TripRequest(
            trip_type="private",
            model="gpt-5.6-sol",
            origin="東京",
            destination="タイ",
            start_date=date(2027, 8, 8),
            end_date=date(2027, 8, 11),
            adults=2,
            children=2,
            budget="60万円",
            interests="家族向け",
        )
        prompt = build_trip_prompt(request)
        self.assertIn("プライベート旅行", prompt)
        self.assertIn("大人2名、子ども2名", prompt)
        self.assertIn("フライト候補", prompt)
        self.assertIn("ホテル候補", prompt)
        self.assertIn("日別旅程", prompt)
        self.assertIn("フライトだけで出力を使い切らない", prompt)

    def test_extract_citations_deduplicates_urls(self):
        citations = extract_citations(DummyResponse())
        self.assertEqual(
            [item["url"] for item in citations],
            ["https://example.com/hotel", "https://example.com/flight"],
        )

    def test_parse_trip_sections_orders_known_sections(self):
        answer = """# Trip Advisor
概要です。

## 1. フライト候補
|候補|価格|
|---|---|
|A|10万円|

## 2. ホテル候補
ホテルA

## 3. 日別旅程
1日目

## 4. 概算予算
50万円
"""
        sections = parse_trip_sections(answer)
        self.assertEqual(
            [section.key for section in sections],
            ["summary", "flights", "hotels", "itinerary", "budget"],
        )

    def test_parse_trip_sections_merges_daily_headings(self):
        answer = """## 旅程サマリー
概要
## フライト候補
便
## ホテル候補
宿
## 8月8日（日）：到着日
ホテルで休憩
## 8月9日（月）：寺院
午前に観光
## 概算予算
60万円
"""
        sections = parse_trip_sections(answer)
        itinerary = next(section for section in sections if section.key == "itinerary")
        self.assertIn("8月8日", itinerary.content)
        self.assertIn("8月9日", itinerary.content)
        self.assertEqual(
            [section.key for section in sections].count("itinerary"),
            1,
        )

    def test_compact_web_search_event(self):
        event = _compact_event(
            {
                "type": "response.web_search_call.searching",
                "sequence_number": 42,
                "action": {
                    "query": "Bangkok family hotels",
                    "sources": [{"url": "https://example.com"}],
                },
            }
        )
        self.assertEqual(event["phase"], "search")
        self.assertEqual(event["sequence_number"], 42)
        self.assertEqual(event["queries"], ["Bangkok family hotels"])
        self.assertEqual(event["source_count"], 1)


if __name__ == "__main__":
    unittest.main()
