FoCo Fuel

**A dietitian in your pocket for Dartmouth students and student athletes struggling to eat nutritionally in the dining hall.**

FoCo Fuel turns Dartmouth's daily dining-hall menu into targeted, training-aware meal suggestions. You tell it how hard your training load is for the day- a hard, easy, or moderate practice, a game day, or a rest day- and it curates 3 complete meals from what's being served at 53 Commons (FoCo) that day. The meals are anchored with protein and emphasize carbohydrates that scale to your training load, in line nutritionist recommendations.
Built by **Madeline LaFata** as a portfolio product

**Demo**

[Watch the demo here:](https://youtu.be/GaPlCgJN6hU)

**The Problem**

Dartmouth students and student athletes struggle to match meal options with training load. The fueling information exists, as FoCo posts a full daily menu with per-item nutrition and Dartmouth staffed nutritionists provide additional resources and support, however these resources are often underutilized. A dietitian appointment is 1:1 and unscalable, and numerous food options are not something a hungry student wants to think about between classes, meetings, and training sessions. 
FoCo Fuel intends to close this gap with the dietitian's recommendation delivered the moment you decide what to put on your plate.

**How it Works**

The core design principle comes from ACSM and ISSN sports-nutrition guidance: carbohydrate is the dial that scales with training load and protein is the constant. Hard training depletes glycogen, so carbohydrates are emphasized as training intensity increases. Protein is included to support repair every day, and vegetables are always recommended. FoCo Fuel applies these principles qualitatively using verbal emphasis and portion cues, never personal calorie or gram targets. This promotes Foco Fuel as a fueling tool, not a dieting one.


When using the app, you pick a date, meal, and how demanding your day is. The app pulls that day's FoCo menu, filters it to what's safe for your dietary needs, and returns two to three complete plates, each with a one-line rationale and a portion cue that shifts with your training load ("go big on the rice — you've earned it today" versus "a normal scoop is plenty").

Architecture: rules own safety, AI owns language
The most important design decision in this project is the division of labor between deterministic rules and the AI model:

Rules (rules.py) own everything that can hurt someone. Allergen and dietary filtering happens in Python, before the AI is ever involved. Each surviving item is tagged by role (protein / carb / vegetable) from its nutrition data. The AI only ever receives a shortlist of items already cleared as safe for that user, and it never sees identity, allergens, or preferences. It cannot reference an item that wasn't pre-approved.

The AI (ai_plates.py) composes and explains meal selection. From the safe shortlist plus the day's training load, it assembles coherent plates and writes the rationale for the meal. Every plate it returns is then re-validated against the shortlist in Python; anything invalid is dropped.

Graceful degradation is built in. If the AI call fails, times out, or returns nothing usable, the app silently falls back to a rules-only plate. The user always gets a safe, sensible recommendation, never an error or a plate with nothing.
This structure means an AI hallucination can't put an allergen on someone's plate. That safety property is designed to hold in the construction of the app and throughout plate curation with AI assistance.

**Project Status**

This is a working pilot-stage build, developed with AI-assisted coding (Claude Code):

Live and working: the full pipeline: menu ingestion, dietary/allergen filtering, role tagging, rules-based plate composition, training-load portion cues, and the browser UI with graceful loading and fallback states.

Built and integrated, currently behind a vendor migration: the AI plate-composition layer is fully wired in and was verified end-to-end producing real multi-plate output. It runs on Google's Gemini API, which is mid-transition to a new key format; the integration is complete and the app falls back cleanly to its rules engine in the meantime.

In progress: Dartmouth migrated its menu platform (from a custom API to Nutrislice) during development. The app now runs on a clearly-labeled sample menu while the new data source is integrated; the new endpoint has been confirmed to expose the per-item nutrition and allergen data the safety layer requires.

The sample-menu state is surfaced honestly in the UI with a visible banner in the demo to never present sample data as live data.

**What This Project Demonstrates**

Scoping a product from a real, validated need with defensible features

PMing an AI feature responsibly by deciding what the model is and isn't allowed to touch, designing for its failure modes, and verifying its output rather than trusting it.

Handling real-world breakage. From a data source that disappeared mid-build and a vendor credential migration, I navigated this challenge without compromising the safety guarantees or shipping something misleading.

**Tech**

Python (standard-library HTTP server), vanilla HTML/CSS/JS front end, Google Gemini API for plate composition. No framework dependencies. API keys are kept server-side and out of version control.
