---
name: ai-image
description: Generate images with gpt-image-2 via Kie AI. Use when the user asks to create, generate, make or render an image, photo, picture or thumbnail (in Thai too, e.g. สร้างรูป, สร้างภาพ, ทำรูป, เจนรูป, เจนภาพ, วาดรูป, ทำภาพปก, ทำทัมบ์เนล), asks to write or improve an image prompt (เขียนพรอมต์รูป, ปรับพรอมต์ภาพ), or wants to fix image output that looks plastic or fake (รูปดูปลอม, ดูเป็นพลาสติก, ดูไม่สมจริง).
---

# ai-image

Generate one image per request with gpt-image-2 through Kie AI. Prompts are
built with the AVB-6 structure below. This file is self-contained.

## Step 0: readiness

Run this once per session, before the first generation:

```
uv run .claude/skills/ai-image/scripts/generate.py doctor
```

If the key is missing, run `doctor --init-env`, tell the user to put their key
from https://kie.ai after `KIE_API_KEY=` in `.env`, and stop. Never ask the
user to paste a key into chat.

## Step 1: read intent

Extract the subject and the action, note implicit style cues, and derive a
three to five word kebab-case working title such as `dog-swimming-tennis-ball`.
Do not ask the user for the title.

## Step 2: build the prompt with AVB-6

Six elements. Every prompt is assembled from all six.

1. **Subject**
   The thing or person in frame: age, clothing, posture, expression, material,
   texture. Never generic. "A girl" and "a bottle" are categories, not
   subjects. "A woman in her sixties with cropped grey hair, wearing a
   paint-stained denim apron" is a subject.

2. **Setting**
   Where the subject is AND what it is rendered as, in one clause:
   `[environment], rendered as [medium]`
   Environment covers location, time of day, weather, background, story
   context. Medium covers the substrate: 35mm film, smartphone photo, oil
   painting, 3D render, risograph print, pencil sketch.
   Example: a cluttered artist's studio at dusk, rendered as a 35mm film photograph
   These are one element on purpose. Decided separately, they contradict each
   other, for example an oil-painting medium carrying photographic detail.

3. **Vantage**
   Shot size plus angle, as one photographic decision.
   Shot size: extreme wide, wide, medium, close-up, extreme close-up, macro
   Angle: eye-level, low angle, high angle, over-the-shoulder, POV, top-down
   Example: close-up, eye-level

4. **Lighting**
   Name the source AND the direction. Both, always. "Cinematic lighting" is
   not lighting, it is a wish. "Warm overhead neon spilling from the left" is
   lighting.

5. **Mood**
   One or two emotional words: melancholic and cozy. tense, watchful.

6. **Style**
   The rendering aesthetic or visual reference.

Assembly:

```
[Subject] in [Setting], [Vantage], [Lighting], [Mood], [Style]
```

### The Prompt Ladder

Do not write all six elements in one pass. Climb one rung at a time and check
each before adding the next. AVB-6 is the checklist; the ladder is the process.

1. Subject: make the seed idea specific
2. + Setting: place it somewhere concrete and decide the medium
3. + Vantage: decide how it is framed
4. + Lighting: name the source and direction
5. + Mood: add one or two emotion words
6. + Style: add a treatment, or skip it if plain realism is the goal

Then show the assembled prompt and ask: "Run this, or want to adjust?"

Their edits are the source of truth. Refining is not rewriting their vision.
If they said "a dog", do not decide it is a golden retriever.

## Step 3: generate

```
uv run .claude/skills/ai-image/scripts/generate.py image \
  --prompt "<AVB-6 prompt>" --title "<working-title>" \
  [--aspect-ratio "16:9"] [--resolution 2K] [--input-image "<ref>"]...
```

Images run without asking for approval. The script prints a JSON line with
`image_path` and `folder`. Always show the user the absolute path.

Parameter limits:

- `--aspect-ratio`: auto (default), 1:1, 3:2, 2:3, 4:3, 3:4, 5:4, 4:5, 16:9,
  9:16, 2:1, 1:2, 3:1, 1:3, 21:9, 9:21
- `--resolution`: 1K, 2K, 4K. Omit unless the user asked. `auto` supports 1K
  only and 1:1 cannot reach 4K, so 2K or 4K always needs an explicit aspect
  ratio.
- `--input-image`: repeatable, up to 16 local files, each 10 MB or less. The
  script uploads them and switches to the image-to-image model by itself.

## Step 4: repair pass

When output looks plastic, fake, generic or flat, do not start over. Find the
AVB-6 element at fault, fix that one element, and state in one line what
changed.

Strip these words on sight in realism mode. They push models toward plastic
3D-render gloss:

> hyper-detailed, hyper-realistic, ultra-realistic, 8k, 4k, unreal engine,
> octane render, masterpiece, award winning, trending on artstation,
> perfect lighting, flawless skin, glamour shot

Inject two or three of these instead, matched to the medium:

> candid iPhone photo, on-the-fly smartphone camera shot, subtle motion blur,
> natural uneven lighting, direct phone flash

> shot on 35mm film, Kodak Portra 400 grain, documentary photo style,
> natural skin texture with visible pores and fine lines, soft atmospheric
> depth, lens dust particles, weathered clothing texture, faded fabric

Symptom to element:

| Symptom | Element to fix |
| --- | --- |
| looks plastic or airbrushed | Style, then Lighting |
| looks flat and lifeless | Lighting (no source or no direction named) |
| right object, wrong feeling | Mood |
| composition is wrong | Vantage |
| looks like a stock photo | Subject is still a category, not a subject |

## Rules

1. One generation per request. Never produce variants preemptively.
2. Never describe a logo, product or a specific person's face in words. Pass
   the real file with `--input-image`. Keep reference images in
   `generations/refs/`.
3. Draft cheap, finish pretty. Iterate at 1K, then rerun the winner at 2K or
   4K with an explicit aspect ratio.
4. Never paste an API key into chat. Keys live in `.env` only.
5. Kie result URLs expire within hours. The script downloads immediately;
   never hand a raw Kie URL to the user as the deliverable.
6. If a model id 404s, the slug drifted. Check https://kie.ai/market, update
   the id, and tell the user what changed.
