# Ollama Speech for NVDA

Use Phonon or LFM3-Audio from an Ollama server, with playback through NVDA's selected audio output. Models and voices are downloaded separately.

## Changelog

### 1.0.1

- Fixed stale Voice and Language filter choices in NVDA Speech settings after changing a language filter or model. Lists and selections now update together without restarting NVDA, while Cancel still restores the previous settings.
- Renamed the add-on folder and implementation modules for Ollama Speech. Existing connections, speech profiles and gestures are retained through a small compatibility entry point.

### 1.0

- First release with Phonon, multilingual Phonon and LFM3-Audio.
- Model, Language filter and Voice selection in Speech settings and the synth ring.
- Streaming audio, rate, rate boost, pitch, volume, pause, cancellation, spelling and Say All.
- Connection panel with model discovery, eSpeak recovery, Help and signed GitHub updates.

## Setup

Requires NVDA 2026.2 or newer on Windows and a reachable Ollama speech server. Both models were tested with Ollama 0.40.12 on an Apple Silicon Mac. Phonon's Ollama packages require Apple Silicon and Ollama 0.40.9 or newer; see the [speech guide](https://docs.ollama.com/capabilities/speech) for current requirements.

On the server, install and run Ollama, then download whichever models you want:

```sh
ollama pull phonon
ollama pull phonon:40m-multilingual
ollama pull lfm3-audio
```

Ollama normally accepts connections only from its own computer. For a separate server, follow Ollama's [network setup guide](https://docs.ollama.com/faq#how-can-i-expose-ollama-on-my-network) and restrict access to a trusted private network. Do not expose it to the public Internet.

1. Install this add-on and restart NVDA.
2. Open **NVDA Settings > Ollama Speech**. Enter the server's address and port, such as `http://your-server:11434`, replacing `your-server` with its hostname or private IP address. `localhost` means the Windows computer running NVDA, not a separate Mac.
3. Press **Refresh models** and check **Connection status**.
4. Press **Use Ollama Speech**. Choose **Model**, optionally narrow the list with **Language filter**, then choose **Voice**, in NVDA's Speech settings or synth ring.

Only installed Phonon and LFM3-Audio models with valid speech capabilities and voice lists are offered. Use **Refresh models** after adding or removing models on the server. **Help** opens this manual.

## Models And Languages

- [Phonon](https://ollama.com/library/phonon): the initial default, for English.
- [Phonon multilingual](https://ollama.com/library/phonon:40m-multilingual): English, French, German, Spanish and Portuguese.
- [LFM3-Audio](https://ollama.com/library/lfm3-audio): its own language and accent presets.

LFM3-Audio can read mixed-language passages without manually changing voices. Language handling comes from the selected model: the add-on does not translate text or automatically select another voice preset. Pronunciation and responsiveness vary with the model, voice and text.

## Speech Controls

Model changes refresh the Language filter and Voice lists without restarting NVDA. **All languages** shows every voice offered by that model. Choosing a language or regional accent narrows the Voice list; it does not translate text or restrict the model's multilingual reading. **Unspecified language** contains the model's default and any voices without verified language metadata. Phonon's character presets appear under **English Character Voices**, separate from its regional English voices.

Language groups use Gradium's published Phonon catalogue and LFM3's language-named voice presets, matched only to voices your server actually offers. No extra download is needed for grouping. Rate, pitch and volume are preserved. Returning to a model restores its previous language filter and voice within the current synth session, if available; otherwise All languages and an available voice are used.

In SoundWave versions with Model support, choose Model, then Language and Voice in its voice options before testing or rendering.

Rate, rate boost, pitch and volume use NVDA's bundled Sonic library. Rate 50 keeps the original speed; rate boost doubles the selected speed. Shift pauses or resumes playback, and Control stops speech and discards cancelled audio. Spelling and Say All follow audio playback.

A cold model may take several seconds to start. Network latency and server load also affect responsiveness.

## Recovery

**Use eSpeak NG if the speech server fails** is enabled by default. A missing model or failed speech request switches the active synthesizer to eSpeak NG.

To reconnect, refresh models in the connection panel and press **Use Ollama Speech**. This uses the saved model if available, otherwise an available Phonon model, or LFM3-Audio if Phonon is absent. You can then choose your preferred Model and Voice.

This network synthesizer is unavailable on secure screens.

## Updates

In the connection panel, choose **Never**, **Hourly** or **Daily** checks; Daily is the default. **Check for updates now** checks manually. Updates come from this add-on's GitHub releases, not the speech server.

The updater verifies the signed package information and download before installation. NVDA then offers its normal restart prompt.

When updating from 1.0, the first update installs a compatibility package. After restarting NVDA, check for updates once more to finish moving from the old `phonon` add-on folder to `ollamaSpeech`. The second step also uses NVDA's normal restart prompt; it does not restart NVDA automatically. Automatic checks can offer this second step unless you selected Never. New installations should use `ollama-speech-1.0.1.nvda-addon` directly. Saved connections and speech preferences are retained. A small legacy synthesizer entry point remains solely to keep existing profiles and gestures working.

## Privacy

Speech text is sent to your configured server. HTTP is not encrypted; use a trusted private connection or encrypted tunnel. HTTPS uses normal certificate validation. API-key authentication and addresses containing credentials are not supported.

The add-on does not save speech text or recordings. Model and voice metadata is cached for the current session; connection and speech preferences use NVDA's normal configuration. Failure diagnostics use NVDA's log without the spoken text.

## Credits And Licenses

- Andre Louis: creator and maintainer, product design, accessibility requirements and real-world testing.
- OpenAI Codex: development, testing and documentation assistance.
- [Gradium](https://github.com/gradium-ai/xn-ptts): Phonon models and the [voice-language catalogue](https://huggingface.co/gradium/phonon-voices) (Apache-2.0; a small metadata snapshot is included, not voice recordings).
- [Liquid AI](https://huggingface.co/LiquidAI/LFM3-Audio-2B): LFM3-Audio.
- [Ollama](https://github.com/ollama/ollama): server and speech API.
- [NV Access and NVDA contributors](https://github.com/nvaccess/nvda): playback, settings and bundled [Sonic](https://github.com/waywardgeek/sonic) processing.
- Andre Louis's Apollo II driver: playback-lifecycle approach and BSD-3-Clause updater.

Add-on code is GPL-2.0-or-later, except the identified BSD-3-Clause updater and Apache-2.0 voice-language metadata. Model licenses are separate: Phonon 31M English is Apache-2.0; Phonon 40M variants are CC BY-NC 4.0, for noncommercial use. LFM3-Audio uses the LFM Open License v1.0. Check each model's linked page for its terms.
