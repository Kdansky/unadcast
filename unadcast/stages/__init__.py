from . import cut, detect_boilerplate, detect_llm, detect_loudness, detect_repeat, detect_voice, download, fuse, prepare, transcribe

# Execution order. The runner finishes each stage for every episode before starting the next, so the fingerprint index
# holds the whole batch before any episode is matched against it.
STAGES = [
    download.STAGE,
    prepare.STAGE,
    detect_loudness.STAGE,
    detect_voice.STAGE,
    detect_repeat.FINGERPRINT,
    detect_repeat.STAGE,
    transcribe.STAGE,
    detect_boilerplate.INDEX,
    detect_boilerplate.STAGE,
    detect_llm.STAGE,
    fuse.STAGE,
    cut.STAGE,
]
