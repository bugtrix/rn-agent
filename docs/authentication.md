# Authentication

This document exists because the terminal makes a claim every time it opens -
"Provider: Anthropic · auth: API Key" - and that claim has to be true. Below is
what each provider *publishes* for a third-party tool like this one, with sources,
and what the agent therefore does.

The short version: **OAuth is used wherever a provider officially offers it, and
where a provider does not, the UI says so rather than dressing a key entry up as
a subscription login.**

## What each provider allows

| Provider | Mechanism for third-party model calls | What rn-agent does |
|---|---|---|
| **Google Gemini** | **OAuth 2.0**, documented for the Gemini API with scopes `cloud-platform` and `generative-language.retriever` ([quickstart](https://ai.google.dev/gemini-api/docs/oauth)). Also accepts an API key. | `/login google` runs a real PKCE + loopback OAuth flow. `Auth: OAuth (Google account)` |
| **Anthropic** | **Console API key only.** Subscription OAuth is reserved for Anthropic's own products; the February 2026 terms state that "the use of OAuth tokens obtained via Claude Free, Pro, or Max accounts in any other product, tool, or service is not permitted" ([summary](https://help.apiyi.com/en/anthropic-claude-subscription-third-party-tools-openclaw-policy-en.html), [tracking issue](https://github.com/anthropics/claude-code/issues/28091)). A `claude setup-token` credential (`sk-ant-oat01-…`) works only with Claude Code and is rejected by the Messages API. A paid Claude subscription does not include API access ([Anthropic support](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-do-i-have-to-pay-separately-to-use-the-claude-api-and-console)). | `/login anthropic` asks for a Console key and prints the reason there is no account option. `Auth: API Key` |
| **OpenAI** | **Platform API key** for model calls. "Sign in with ChatGPT" (launched August 2026) is an *identity* provider: a partner app receives name, email and picture ([OpenAI help](https://help.openai.com/en/articles/20001410-sign-in-with-chatgpt)) - it does not grant API access on a ChatGPT subscription, and routing through Codex tooling is not a supported path ([analysis](https://developer.puter.com/tutorials/openai-oauth/)). | `/login openai` asks for a platform key and explains the distinction. `Auth: API Key` |
| **Ollama** | Runs locally; there is no account and no credential. | `Auth: None (local)` |
| **Claude on Vertex AI** | **Google OAuth** - Anthropic publishes Claude on Google Cloud, and Google Cloud authenticates with the same OAuth flow as Gemini ([Anthropic](https://platform.claude.com/docs/en/build-with-claude/claude-on-vertex-ai), [Google](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/partner-models/claude)). | `login vertex` signs in with your Google account and calls Claude with no Anthropic key. `Auth: OAuth (Google account)` |
| **Cursor** | **The Cursor CLI's own login.** `rn-agent login cursor` runs `cursor-agent login`, which opens Cursor's own sign-in page and stores the session in Cursor's config; rn-agent never reads that credential. A `CURSOR_API_KEY` is accepted for CI ([CLI auth](https://cursor.com/docs/cli/reference/authentication)). | `Auth: Cursor CLI session`. No credential is copied, scraped or re-stored |

Three consequences worth stating plainly:

1. **A Claude Pro/Max subscription cannot pay for this agent's requests.** Any tool
   that claims otherwise is using a mechanism Anthropic has prohibited. rn-agent
   does not implement one, and the note in `/status` and `/login` says why.
2. **Google OAuth authorises your own Cloud project**, not a consumer Gemini
   subscription - Google also withdrew the consumer-subscription CLI pattern in
   February 2026. The tokens are yours, the billing is yours, and the flow is the
   documented one.
3. **There is a browser login that reaches Claude**, and it is `login vertex`:
   a Google consent screen, no API key, Claude models, billed to your Cloud
   project. What it is *not* is a Claude.ai subscription session - it is a
   different product with a different bill.

## What is deliberately not implemented

None of the following exist anywhere in this repository, and none are planned:

- browser-cookie extraction or session-cookie reuse;
- reading credentials out of another vendor's CLI or desktop app;
- password collection;
- private or reverse-engineered endpoints;
- impersonating another official client's user agent or OAuth client id;
- proxying third-party traffic through a subscription endpoint.

The Cursor row above is worth separating from the second bullet, because the
distinction is the whole point. rn-agent does **not** read Cursor's stored
credential; it runs `cursor-agent`, which authenticates itself, exactly as a
developer would in a shell. Invoking a tool through its own documented interface
is not the same act as reading the secret it keeps - one is supported, the other
is what that bullet forbids.

The agent also never invents usage figures. `/status` reports the tokens **this
project has spent**, taken from its own `ai_usage` table, and says so - because no
provider in the table publishes remaining quota through a supported interface.

## The OAuth flow, concretely

`/login google` performs the standard installed-application flow:

1. A PKCE verifier is generated (S256; the challenge is what binds the exchange
   to this process, since an installed app cannot keep a secret).
2. A one-shot HTTP listener binds `127.0.0.1` on an ephemeral port.
3. The browser opens the provider's own consent screen. On a machine with no
   browser the URL is printed instead.
4. The redirect is accepted **only** if it carries back the `state` this run
   generated; anything else is refused, so another local page cannot feed the
   agent a code.
5. The code is exchanged for tokens over TLS, directly from this process.
6. Tokens go to the OS keychain. Nothing is printed, nothing is logged, and the
   listener closes.

Access tokens are refreshed automatically a little before expiry, and the
refreshed token is written back so a session lasts as long as the refresh token
does.

### No browser? The device grant

A container, a CI runner or an SSH session has no browser to open, and waiting
on a loopback redirect there is a hang, not a login. So when no browser is
detectable - or with `--device` - the agent uses the RFC 8628 device grant
instead: it prints a short code and a URL, you approve on a machine that *does*
have a browser, and this process polls until the provider says yes.

```
  → No browser here - signing in with a device code
  → Open https://www.google.com/device
  → Enter the code WXYZ-1234
```

Nothing is scraped and no password is handled: it is the same consent screen,
reached from another device. `authorization_pending` is a wait, `slow_down`
widens the interval, and an expired code says to run the login again.

### The OAuth client

Google's flow needs an OAuth client, and rn-agent does not ship one. That is a
deliberate choice, not an omission: an embedded client id in a public CLI makes
every user share one identity and one consent screen, and the client belongs to
whoever owns the project being billed.

```bash
# Google's quickstart hands you a client_secret.json - point at it
rn-agent login google --client-file ~/Downloads/client_secret.json

# or pass the two values yourself (Desktop app credentials)
rn-agent login google --client-id <id>.apps.googleusercontent.com --client-secret <secret>
```

Both `installed` and `web` blocks are accepted, and a file with no `client_id`
is refused rather than half-registered. The client id and secret are stored in
the keychain alongside the session, so later logins need no arguments.

### One Google account, two providers

`google` (Gemini) and `vertex` (Claude on Google Cloud) are the same Google
account, so they share one stored session: signing in for either connects both,
and signing out of one signs out both. That is what `shares_session_with` in
`PROVIDER_AUTH` means, and `whoami` lists both as stored.

Vertex additionally needs to know which project pays, because the model lives at
a project-scoped URL:

```bash
rn-agent login vertex --client-file ~/Downloads/client_secret.json \
  --cloud-project my-project --region us-east5
```

`--cloud-project` and `--region` are written to config (`ai.project`,
`ai.region`); `--region` defaults to Google's `global` endpoint. Vertex model ids
carry a release date (`claude-sonnet-4-5@20250929`) and which ones you may call
depends on your project's Model Garden grants, so the agent ships no catalogue
and no default model - it asks for one and says why.

One honesty note: `login vertex --check` confirms that a Google session and a
project exist, and says it did **not** call the API. Every Vertex Claude request
is a billable prediction and `rawPredict` has no free probe, so a live
"verified" would either be a lie or a charge.

## Where credentials live

| What | Where | Never |
|---|---|---|
| API keys | OS keychain (macOS `security`, Secret Service, Windows DPAPI) | the project, a log, `config.yaml` |
| OAuth tokens | the same keychain, under `<session>-oauth` | the project, a log, `config.yaml` |
| OAuth client id/secret | the same keychain, under `<session>-oauth-client` | the project, a log, `config.yaml` |
| Which providers have a credential | `~/.config/rn-agent/credentials.json` (an index; no secrets) | — |

`<session>` is the provider name, except where providers share one account:
`vertex` stores nothing of its own and reads Google's slot. That is why signing
out of Gemini also disconnects Claude-on-Vertex - there is one token, not a copy.

On a machine with no keyring (a container, CI), the file backend stores
credentials `0600` under `~/.config/rn-agent` and reports `backend_secure:
false`, which the terminal surfaces as a visible warning. An environment variable
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`) always wins over stored
credentials, and `/whoami` names which source was used - "I exported a key" and
"a key is in my keychain" are different facts.

Token payloads are base64url-encoded before they reach a backend. That is not
obfuscation - it is because a keychain stores one opaque string per account, and
macOS passes it through a `security -i` script where a JSON blob's spaces and
quotes would corrupt the write.

## Adding a provider's OAuth later

When a provider ships an official OAuth program for third-party tools:

1. Add its endpoints and scopes as a client builder in `auth/methods.py`. Set
   `device_url` if it publishes an RFC 8628 endpoint - that is all the device
   grant needs.
2. Change its row in `PROVIDER_AUTH` (`auth/manager.py`) to `AuthMethod.OAUTH`
   and drop the `unsupported_note`. Set `shares_session_with` if it signs in
   with an account another row already owns.
3. If its API distinguishes a key header from a bearer header, extend the
   provider's constructor the way `GoogleProvider(oauth=...)` does, and report it
   from `SessionManager._provider_extras`.

So the day Anthropic opens third-party OAuth, `login anthropic` becomes one
edited row in that table - not a new code path.

Nothing in the terminal UI changes: the provider picker, the status bar and
`/whoami` all read the capability table.
