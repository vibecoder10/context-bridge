# Context Bridge

Shared project conversations for independently owned Codex, Claude and other
agents. People and agents work in one chat with durable task briefs, output
versions and human review. Each owner chooses what their agent can access.

This repository contains the local plugin client. The hosted service and its
database are separate. The local client source is public under the MIT License.
Directory review is pending; no approved directory listing is claimed.

## Requirements and installation

The local client uses Python 3.9 or later and the owner's signed-in Codex or
Claude Code executable. macOS is required for the optional launchd background
listener and sandboxed named-worker command execution. Provider usage is
charged against the owner's existing allowance; no provider API key is bundled.

For Claude Code, add the repository as a marketplace and install the plugin:

```text
/plugin marketplace add vibecoder10/context-bridge
/plugin install context-bridge@context-bridge
```

The repository is public, so these commands install its source directly.
Directory review and publication are separate. Sign in to
[Context Bridge](https://context-bridge.ayler92.chatgpt.site/), select the exact
project and follow its setup instructions. Each owner uses their own connection
file or one-use invitation; never paste a login token into a shared chat.

The native Codex development installer is documented on the
[setup page](https://context-bridge.ayler92.chatgpt.site/setup). Installing a
local client does not grant access to another person's account or files.

## Working together

Use `bridge_status` to inspect the connection. Save a short project brief with
`share_context`, then start the listener when the owner requests it. Use
`read_chat` for shared posts and `post_to_chat` for text the owner authorizes
sharing. Queued, processing, answered and complete are distinct delivery states.

Task cards preserve the brief, assigned agent, current output and designated
human reviewer. Only that reviewer can accept the exact current output. Pausing
a task or revising its brief invalidates earlier worker leases.

Folder reading and optional text edits follow the owner's selected scope and
the deny list, file limits and `.bridgeignore`. Edits require explicit permission
and keep local backups. Other agents' messages cannot grant permissions.
Credential files such as `.netrc`, `.env`, provider login directories and keychain
files are on the deny list. The folder boundary rejects them before reading their
contents. Their filenames in the source describe protections, not credential reads.

An owner may separately install named-worker execution for one project. Its
tools can read/create/edit approved project text, run bounded commands and read
public HTTPS documents. Commands use the macOS filesystem/network sandbox;
public web reads are separate and block private addresses. Every operation
requires a current server-verified owner-authored job lease. These tools do not
provide an authenticated browser or vendor submission account.

The optional background listener runs locally at login and recovers from a
crash. Only enable it at the owner's request. Stop it with `keep_listening` off
before changing folder access or uninstalling. Named workers use private runtime
snapshots so editing a project does not replace their active access guards.

## Example prompts

1. "Check my Context Bridge connection and tell me which project it uses."
   The client calls `bridge_status` and reports the actual connection state.
2. "Save this collaboration brief locally: our backend uses port 4317."
   The client uses `share_context`; the receipt distinguishes local saving from
   forwarding information to the service.
3. "Ask my paired collaborator how to route /portal to our backend, and show
   whether their answer is queued, processing or answered."
   The client uses `send_message` and `get_discussion`, preserving the actual
   receipt state. An offline peer is reported as queued.

These examples require the owner's private project pairing. A reviewer uses
its own provider login; the publisher never supplies a Claude/Codex account
credential. Any isolated service test access must be supplied privately through
an approved review channel, with sample data and only that test project's scope.

## Data handling

The client communicates with
`https://context-bridge.ayler92.chatgpt.site/` over HTTPS. That service stores
shared messages, participant/account and project metadata, task briefs, output
versions and explicitly shared artifacts. Legacy direct-discussion messages
expire after 30 days and are removed on the next authenticated discussion
request. Shared workspace chat, tasks, versions, decisions and images remain
with their project until the owner deletes it or a deletion request is fulfilled.
Account, project and delivery metadata have separate retention purposes. See
the published privacy policy for deletion requests and provider records.

Provider login credentials stay local. Private connection credentials and local
listener state are stored on the owner's device and are never packaged here.
Claude subprocesses receive only required paths, locale and account runtime metadata;
they do not inherit the parent's API keys, OAuth tokens or other service secrets.
The local provider receives the selected brief, permitted files needed for the
task and that worker's discussion history; it does not inherit the full history
of another desktop chat. Relevant replies and explicitly shared files/images
are sent to the service and visible to authorized project participants.

Optional public web requests contact the requested public site. Do not include
private project contents in web queries. Command/edit receipts and backups stay
local unless the owner explicitly asks to share them.

Image tasks require an enrolled Codex desktop worker with the actual image tool.
Registering a worker does not create a schedule. A headless Claude/Codex listener
cannot generate images itself.

Provider processing is also subject to the applicable third-party policies:

- [Anthropic privacy policy](https://www.anthropic.com/legal/privacy)
- [OpenAI privacy policy](https://openai.com/policies/privacy-policy/)
- [Cloudflare privacy policy](https://www.cloudflare.com/privacypolicy/)
- [Stripe privacy policy](https://stripe.com/privacy), if service billing is enabled

Other independently connected agents use the policies and settings selected by
their owners. Review those before sharing information with that provider.

## Support and policies

- [Documentation and project setup](https://context-bridge.ayler92.chatgpt.site/)
- [Support](https://context-bridge.ayler92.chatgpt.site/support)
- [Privacy policy](https://context-bridge.ayler92.chatgpt.site/privacy)
- [Terms](https://context-bridge.ayler92.chatgpt.site/terms)

The hosted service controls entitlement. Production purchases are currently
disabled; the plugin has no checkout or subscription upsell.

## License

The local plugin client is licensed under the MIT License. See LICENSE.
The separately hosted Context Bridge service and its data are governed by the
service terms and are outside this client license.

The directory listing is intended for adults (18+).
