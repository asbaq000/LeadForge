"""Offline sanity check for the scoring rules - no browser, no network.

Runs a labelled set of realistic Threads posts through the scorer and reports
how many are classified correctly. Use this after editing config.yaml to make
sure you have not loosened the filter into noise or tightened it into silence.

    python scripts/selftest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from threads_leads.config import load_settings  # noqa: E402
from threads_leads.enrich import extract_emails, extract_links, suggest_opener  # noqa: E402
from threads_leads.scorer import LeadScorer  # noqa: E402

# (text, should_be_a_lead)
SAMPLES: list[tuple[str, bool]] = [
    # ---------- should qualify ----------
    ("Looking for a developer who can build an AI agent that reads our support "
     "inbox and drafts replies. Budget is around $3k. DM me if you can help.", True),
    ("Anyone know how to automate moving leads from a Google Form into our CRM? "
     "Doing it manually is killing me.", True),
    ("Need help automating my invoicing workflow in n8n. Been stuck on the webhook "
     "step for two days.", True),
    ("We're hiring a Python developer for a scraping project - need to pull product "
     "data from a few hundred sites daily.", True),
    ("My agency is drowning in manual reporting. Is there a way to use AI to build "
     "client reports automatically? Recommendations for tools welcome.", True),
    ("Zapier is not working for what I need. Looking for someone who knows make.com "
     "to rebuild my whole onboarding flow.", True),
    ("Can anyone build a chatbot for my Shopify store that handles order status "
     "questions? Willing to pay.", True),
    ("Small business owner here. Spending 10 hours a week on data entry between two "
     "systems. Any tips on how do I automate this with an API?", True),
    ("Struggling with our RAG chatbot - retrieval keeps returning garbage. Need an "
     "LLM engineer to take a look.", True),
    ("Trying to figure out how to connect OpenAI to our internal spreadsheet so the "
     "team can query it. Anyone able to help? reach me at ops@brightpath.co", True),

    # ---------- should be rejected ----------
    ("I build AI automations for founders. DM me for a free audit of your workflow. "
     "Link in bio.", False),
    ("Here's how I made $10k last month with AI agents. Follow for more.", False),
    ("gm", False),
    ("Just shipped a new n8n workflow for a client. Loving how fast this stack is.", False),
    ("My course on automation opens Monday. Join my cohort and I'll show you the "
     "exact system.", False),
    ("Looking for a good plumber in Austin, anyone have recommendations for one?", False),
    ("Free guide: 20 Zapier templates every agency needs. Comment TEMPLATE and I'll "
     "send it over.", False),
    ("Crypto airdrop going live tomorrow, follow back for details.", False),
    ("AI is going to change everything in the next five years. Wild times.", False),
    ("We offer custom chatbot development for ecommerce brands. Book a call with me "
     "this week.", False),

    # ---------- real sellers pulled from live Threads, kept as regression cases ----------
    ("Hi @di_jahrie If you're looking for someone who understands more than just the "
     "setup, we can help. We've built workspace/CRM systems and automations for "
     "agencies, including a recent one for a marketing team.", False),
    ("If you need help with development, deployment, automations or security.. "
     "I am your guy", False),
    ("The fastest way to get more students into your dojo. You don't need a bigger ad "
     "budget to beat the school down the road.", False),
    ("Looking for Digital Marketing Agency in need of a workspace set up. Comment or Dm",
     False),
    ("I can build you a full AI automation system that handles your leads end to end. "
     "Open to projects this month.", False),
    ("Our team specialises in n8n and Make automations for agencies. Reach out to us "
     "if you want to scale.", False),

    # ---------- second live pass: genuine leads that MUST keep qualifying ----------
    ("I need a developer to work with me on a freelance project. Skill required : "
     "Python Next js Or both. Duration : till November 2026", True),
    ("I need help with my marketing, do you know any guns in this space I should be "
     "speaking to? - help with social media posts & reels - plan a structure, "
     "automations and funnels - run ads and manage", True),
    # Real buyer with a real budget, but asking for Shopify storefronts - that is
    # web design, not AI/automation. It only ever scored because the handle
    # "@ai.sahilbeniwal" made "\bai\b" match. Labelled False deliberately: widening
    # `domain` to catch generic web-dev work would flood the sheet.
    ("@ai.sahilbeniwal I need 2 shopify websites in 4 weeks, Jewellery and clothing "
     "brand, Budget: 25k - 30k Please send me a quotation in my DM", False),

    # ---------- second live pass: sellers/noise that reached the sheet ----------
    ("Imagine 90 days from now having another stream of income coming in from a "
     "business you finally stopped thinking about and actually started. I'm looking "
     "for 5 people I can personally help build and automate a credit business.", False),
    ("If you need help with your website, Ads setup and automation I am an expert in "
     "all of these, kindly let me know...", False),
    ("Turning ideas into working products. If you're building something and need a "
     "developer or AI/automation person to jump in, my DMs are open.", False),
    ("Assalamualaikum & hi everyone! I'm currently looking for an internship "
     "opportunity in Software Development / Web Development / IT. Bachelor of "
     "Computer Science (Hons.), UiTM. Internship period: 7 Sept - 11 Dec", False),
    ("Stop sending emails manually. With Email Automation, you can reach the right "
     "customers at the right time-automatically. From welcome emails and follow-ups "
     "to promotions and reminders.", False),
    ("if you are still wasting time re-prompting AI, this is the fix. An 18-page "
     "step-by-step checklist for setting up n8n + Ollama from zero to automated "
     "posting.", False),
    ("You were proud of doing tasks manually instead of using AI. Losing 4hrs on "
     "something you could automate to a 10min task? These AI tools will help you "
     "automate tasks: n8n, Make", False),
    ("I STOPPED building AI workflows from scratch. If I need different or new "
     "workflows, my agents know EXACTLY where to look. Here's the automations I've "
     "set up to help me with this. Sharing it here in case it helps.", False),
    # Note the last line - "Need help with YOUR x? Send us a DM" is an advert.
    # "I need help with MY x" is a lead. The possessive is the whole signal.
    ("Cash flow issues aren't always caused by low sales, sometimes they're caused by "
     "delayed collections!\n\nImplementing clear payment terms and automated follow-ups "
     "keeps your cash flowing predictably every month.\n\nNeed help with your "
     "accounting? Send us a DM.", False),
    ("The longer you write code manually it becomes quicker to write it, often times "
     "finding non-ai ways of automating repetitive tasks or snippets, leading for "
     "libraries and frameworks. AI isn't a solution to everything.", False),
]


def main() -> int:
    settings = load_settings()
    scorer = LeadScorer(settings)

    correct = 0
    false_pos: list[str] = []
    false_neg: list[str] = []

    print(f"min_score = {settings.min_score}\n")
    print(f"{'exp':<5}{'got':<5}{'score':<7}{'level':<7}post")
    print("-" * 100)

    for text, expected in SAMPLES:
        v = scorer.score(text)
        got = v.level != "REJECT"
        hit = got == expected
        correct += hit
        flag = " " if hit else "X"
        preview = text.replace("\n", " ")[:58]
        print(f"{flag}{'Y' if expected else 'n':<4}{'Y' if got else 'n':<5}"
              f"{v.score:<7}{v.level:<7}{preview}")
        if not hit:
            (false_pos if got else false_neg).append(preview)

    total = len(SAMPLES)
    print("-" * 100)
    print(f"\n{correct}/{total} correct ({correct / total:.0%})")

    if false_pos:
        print(f"\nFalse positives ({len(false_pos)}) - sellers/noise that slipped through:")
        for t in false_pos:
            print(f"  - {t}")
        print("  Fix: add a pattern to scoring.seller_negative or raise run.min_score")
    if false_neg:
        print(f"\nFalse negatives ({len(false_neg)}) - real leads that were dropped:")
        for t in false_neg:
            print(f"  - {t}")
        print("  Fix: add a pattern to scoring.intent_high/domain or lower run.min_score")

    # Spot-check the enrichment helpers on a rich sample.
    sample = SAMPLES[9][0]
    v = scorer.score(sample)
    print("\nEnrichment spot-check")
    print(f"  emails  {extract_emails(sample)}")
    print(f"  links   {extract_links('check out https://brightpath.co/pricing for context')}")
    print(f"  opener  {suggest_opener(sample, v.topics, 'Dana')}")

    return 0 if correct == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
