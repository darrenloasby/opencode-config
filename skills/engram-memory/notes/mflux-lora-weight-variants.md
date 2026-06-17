# LoRA Weight Variants Feature

**Date**: 2026-06-14
**Topic**: mflux/lora-weight-variants

## What
Added comma-separated weight variants textbox to MFLUX Advanced Generate tab. Generates one image per weight value.

## Where
- `/Users/ai/Source/MFLUX-WEBUI/frontend/components/advanced_generate.py` — Added `lora_weight_variants` Textbox, passed to callback
- `/Users/ai/Source/MFLUX-WEBUI/backend/flux_manager.py` — `generate_image_gradio` added variant loop, rebuilds model per weight

## Also done this session
- Fixed gen_all.py lora paths (wrong filenames) and API port (1236→7861→7870)
- Started API server on port 7870
- Generated 12 anatomy-2 + coachbate images (both chars, 4 strengths each)

## 2026-06-14 Update — Rendering Bug in Gradio 6.15.2

LWV textbox defined in advanced_generate.py, exists in Gradio config (ID 457, visible: true), 
but renders as EMPTY div in DOM. Gradio 6.15.2 frontend doesn't render this particular gr.Textbox.
No JS console errors. Not a Python bug — Gradio 6.x rendering incompatibility.

Workaround ideas:
- Clear __pycache__ and restart
- Try container=True
- Downgrade Gradio to 5.x
- Use gr.HTML as fallback
