---
title: "AHG Archives for AI Assistants - Quick Start"
---

How to connect an AI assistant to an AHG catalogue, check that the connection works, ask good questions, and suggest improvements to records for an archivist to review.

| Field | Detail |
|---|---|
| Document title | AHG Archives for AI Assistants - Quick Start |
| Prepared for | Archivists, cataloguers and researchers using AHG catalogues |
| Author | Dr Johan Pieterse |
| Organisation | The Archive and Heritage Digital Commons Group (Pty) Ltd |
| Version | 1.3 |
| Status | Pilot |
| Date | 26 October 2026 |
| Service address | https://mcp.theahg.co.za/mcp |
| First catalogue | PSIS - AHG Extensions for AtoM demonstration catalogue |

: Document control

# 1. What this is

AHG Archives is a connector that lets an AI assistant search and read an archival catalogue directly, instead of guessing from whatever it finds on the open web. Every answer it gives from the catalogue carries a link to the record it came from, so you can check it.

Staff can go one step further and ask the assistant to suggest access points, dates and better descriptive text. Those suggestions are never saved to the record. They go into the catalogue's review screens, where an archivist accepts, corrects or rejects each one.

The pilot catalogue is PSIS, the AHG demonstration site. One connection can read more than one catalogue: you choose which one in the conversation (section 3.8).

# 2. What it can and cannot do

| It can | It cannot |
|---|---|
| Search descriptions by keyword | See drafts, embargoed, classified or otherwise restricted records |
| Read a description in full: dates, names, subjects, places, notes, extent | Show anything a public visitor of the catalogue could not see, unless you connect with your own staff key |
| Show where a record sits: the path up to its fonds, and the records below it | Change a record directly |
| Read authority records (people, families, organisations), and find existing ones by name | Delete or move anything |
| Read the text held for a record's files, and look at a page image | Work without a link back to the record |
| Point to a record's images in a IIIF viewer | Approve its own suggestions |
| Suggest access points, dates, field text, relationships between people, transcriptions and translations, with evidence, for review (staff only) | Make more than 200 suggestions an hour for one person |
| Work through every record below a fonds or series, a batch at a time | Send suggestions to any catalogue but its home one |
| Search several catalogues at once, and read each one | |

: Scope

# 3. Connecting

There are two addresses:

| Address | For | Sign-in |
|---|---|---|
| https://mcp.theahg.co.za/public/mcp | Anyone: research and trying the service | None |
| https://mcp.theahg.co.za/mcp | Catalogue staff: everything above, plus suggestions | Your own catalogue API key |

: Connector addresses

## 3.1 Read only (anyone)

1. In your assistant, open the settings for connectors (usually Settings, then Connectors) and add a custom connector.
2. Name it **AHG Archives** and give it the address **https://mcp.theahg.co.za/public/mcp**.
3. Leave authentication and any advanced settings empty.

You will see what any visitor to the public catalogue sees.

## 3.2 Get your catalogue API key (staff)

1. Sign in to the catalogue (for the pilot, https://psis.theahg.co.za) and open **/api/keys**.
2. Name the key, for example "AI assistant", and tick **Read** and **Create and update** only. Leave Delete and Batch unticked; the assistant never needs them.
3. Create it and copy it at once: it is shown only once.

The key can do no more than you can. Keep it private; if it is ever exposed, revoke it at /api/keys, which also disconnects any assistant using it, and create another.

## 3.3 Browser-based assistant (staff)

1. Add a custom connector as in 3.1, but with the address **https://mcp.theahg.co.za/mcp**. Leave the advanced settings (client ID and secret) empty.
2. Choose **Connect**. A page titled "Connect your assistant to ..." opens.
3. Paste your API key and choose **Connect** once. After a few seconds you are returned to your assistant, connected.

The assistant never sees your key: our service keeps it, encrypted, and gives the assistant a token of its own that expires and renews itself. If the page says the sign-in link has expired, start again from the assistant's Connect button; each attempt gets a fresh link.

## 3.4 Desktop app (staff)

Desktop apps can send the key directly. Open the app's MCP configuration file (in many apps: Settings, then Developer, then Edit Config) and add the entry below. This is the standard MCP client format; `mcp-remote` is a small open-source bridge that adds your key to each request.

```json
{
  "mcpServers": {
    "ahg-archives": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.theahg.co.za/mcp",
               "--header", "Authorization:${AHG_AUTH}"],
      "env": { "AHG_AUTH": "Bearer YOUR-KEY-HERE" }
    }
  }
}
```

Replace `YOUR-KEY-HERE` with your key (keep the word `Bearer` and the space before it), save, and fully restart the app. This needs Node.js on your computer. The key sits in the `env` line, not in `args`, because some systems split an argument that contains a space. If the file already lists other servers, add only the `"ahg-archives"` entry inside the existing `mcpServers`.

## 3.5 Tool permissions

Most assistants let you choose, per tool, whether it may run without asking. A sensible setting:

| Tools | Setting |
|---|---|
| List catalogues, Search records, Get record, Get children, Get authority, Search authorities, Get IIIF manifest, Get record text, Get record image, Work through series, List my proposals | Always allow: they only read |
| Propose access points and dates, Propose relationships, Propose description change, Propose transcription, Propose translation | Needs approval: you see each suggestion before it is sent for review |

: Recommended tool permissions

## 3.6 A personal read-only key (trying it on your own agent)

The public address in 3.1 needs no key and is the easiest way to look around. If you want to load AHG Archives into your own agent or application and test it properly, ask AHG for a personal read-only key.

Write to support@theahg.co.za with your name, your organisation, what you would like to try, and for how long. AHG issues the key to an account with no staff rights, so it reads exactly what a visitor sees and nothing more. It has an end date and a daily limit, and AHG can switch it off at any time.

Use it like a staff key: in a browser-based assistant as in 3.3, or in a desktop app or your own client as in 3.4, with the address https://mcp.theahg.co.za/mcp. Over the public address it gives you your own limits instead of a shared one, and your calls can be told apart when you ask how your tests went.

Suggestions are refused with a read-only key (the message says a write scope is required): suggesting is for catalogue staff.

## 3.7 Limits

Every key has an hourly limit, and a personal key also has a daily one. When a limit is used up the assistant is told so in plain words, with the time it resets; nothing is lost, so ask again later. Each person may also make at most 200 suggestions an hour, so that no single session floods the review screens.

## 3.8 Choosing a catalogue

The service has a home catalogue (for the pilot, PSIS) and may read others. Ask the assistant which ones there are:

> Using AHG Archives, which catalogues can you search?

Then name the one you want, or ask for all of them:

> Using AHG Archives, search the Example Municipal Archives catalogue for the 1936 floods.

> Using AHG Archives, search every catalogue for Engelbrecht and say which catalogue each record is in.

Each result says which catalogue it came from and links to the record there. Other catalogues are read as a public visitor would see them, even when you are signed in: your key belongs to the home catalogue and is never sent anywhere else. Suggestions always go to the home catalogue, so ask for them only on its records.

# 4. Check that it works

Ask:

> Using AHG Archives, search for Engelbrecht and give me the links.

A working connection returns records such as "Engelbrecht Family Bible" and "Engelbrecht Family Fonds", each with a link starting https://psis.theahg.co.za/. The assistant should show that it used the AHG Archives tool.

If it answers from general knowledge without links, it did not use the connector. Ask again and name it: "Use the AHG Archives tool."

# 5. Asking good questions

Always name the connector, using the name you gave it when you added it (this guide assumes "AHG Archives"). Assistants do not use a connector unless they think the question needs it, and naming it removes the doubt. Check too that the connector is switched on for the conversation; most assistants have a tools or connectors control beside the message box.

| You want | Try |
|---|---|
| Records on a topic | "Using AHG Archives, find descriptions about farm life in the Karoo." |
| One record in full | "Using AHG Archives, open 'The Call of the Karroo' and summarise it with its link." |
| Context | "Using AHG Archives, which fonds does 'Hendrik and Debora Engelbrecht' belong to, and what else is in it?" |
| A person or organisation | "Using AHG Archives, read the authority record 'Historical' and summarise it." |
| Images | "Using AHG Archives, does this record have images I can open in a IIIF viewer?" |

: Sample questions

Ask for the links. If the assistant states a fact without one, ask where it came from.

# 6. Suggesting improvements (staff key)

Ask the assistant to read a record first and base its suggestions on the record's own text, or on sources it names.

> Using AHG Archives, open 'The Call of the Karroo', then propose places and dates for it. Give the evidence for each.

> Using AHG Archives, read 'The Call of the Karroo' and propose an archival history. Check the web for the film's production and give the sources.

What happens next:

1. **Access points and dates** go to **AI > NER review** in the catalogue. Each suggestion shows the assistant's evidence and links to its sources. You can edit the value, link it to an existing authority record or term (the screen suggests close matches), create a new one, or reject it.
2. **Field text** (scope and content, archival history, extent and medium, arrangement, conditions of access, conditions of reproduction) goes to **AI > Suggest review**. You see the current and the suggested text side by side, with the evidence and sources, and you can edit before approving.
3. **Nothing changes on the record until you approve.** Every suggestion records who asked for it.

Before suggesting a name or place, the assistant can look it up among the catalogue's existing authority records. When it finds the same person, it says so, and **Authority Resolution** lists that record first, marked *Proposed*, so linking it takes one click.

Three more kinds of suggestion arrive in the same review screens:

- **Relationships** between people or organisations that a record states, such as a marriage or parent and child in a family register, go to **AI > Relationship review**. Approve links both names to authority records first; once approved, the relationship shows on both records.
- **Transcriptions** of a scanned page with no text go to **AI > Suggest review**, with the page image beside the proposed text. Once approved, the transcription is the file's text and the record can be found by it.
- **Translations** of a field into another language also go to **AI > Suggest review**, labelled with the language.

To go through a whole fonds or series, ask the assistant to work through it: it takes the records below a batch at a time and keeps a running count of what it suggested where. Ask "Using AHG Archives, list my proposals" to see what became of your suggestions; the assistant uses the same list to avoid repeating one that was rejected.

Suggesting the same value twice does nothing: the catalogue skips what is already waiting or already linked.

# 7. Show and tell: a client's own catalogue

To show an institution what the connector does with their own records, add their catalogue for a few days. No installation is needed on their side.

1. Ask them for their catalogue's web address and a **read-only API key** for an account with no staff rights. On AtoM with the AHG extensions, they create it at **/api/keys** with Read only; on standard AtoM, it is the account's REST API key (the REST API plugin must be switched on). Either way the connector then sees only what a visitor sees.
2. Open **https://mcp.theahg.co.za/catalogues**. Enter your own catalogue API key (an administrator or editor of the home catalogue), a name for theirs as the assistant should show it, its address and their key, and choose **Add**.
3. In the assistant, ask "Using AHG Archives, which catalogues can you search?" Their catalogue is listed, and every question can now name it.

The catalogue is read only: nothing is ever suggested to it. It is removed automatically after seven days, or at once with **Remove by name** on the same page. Addresses inside AHG's own network are refused.

On standard AtoM the connector can search and read descriptions and names, and work through a series. Authority records, file text and images need the AHG extensions.

# 8. Good practice

- **Check web sources.** Websites vary in quality, and assistants can be confidently wrong. Approve a web-sourced fact only when you have looked at the source.
- **Mind personal and restricted information.** Text you send to an assistant leaves the institution. Do not paste restricted content into the conversation; the connector itself never returns restricted records.
- **Keep one question to one record** when suggesting, so the evidence stays clear.
- **Cite the catalogue.** When you use an answer, cite the record link, not the assistant.

# 9. Troubleshooting

| Message or symptom | What it means |
|---|---|
| "Proposing changes needs you to be signed in" | You are on the read-only address. Connect to https://mcp.theahg.co.za/mcp with your key (sections 3.2 to 3.4). |
| "This sign-in link has expired" | Each sign-in link works once and for ten minutes. Start again from the assistant's Connect button. |
| "That key was not accepted" | The key is wrong, revoked, or lacks the Read scope. Create a new one at /api/keys. |
| "No visible record with that reference" | The record does not exist, or you may not see it. Both answer the same, on purpose. |
| "Not authorised" when suggesting | Your account may not edit that record. |
| The assistant answers without links | It did not use the connector. Name it in the question. |
| "Write scope required" when suggesting | You are using a read-only key (3.6). Suggesting needs a staff key. |
| "... limit ... is used up" | Your key's hourly or daily limit, or the 200 suggestions an hour, is reached (3.7). Try again after the time given. |
| "No catalogue called ... here" | The name does not match. Ask which catalogues there are (3.8); a demo catalogue may have lapsed after seven days. |
| "This catalogue's API does not ..." | That catalogue runs standard AtoM, which does not offer it (section 7). |
| The connector cannot be reached | Try again shortly. If it persists, contact AHG. |

: Troubleshooting

# 10. Behind the scenes

The connector reads the catalogue only through its public interface, with the same access rules as the website: published records only, and nothing an embargo, security classification, donor restriction or cultural protocol hides. Personal information is redacted as it is on the site. Each call is logged, so the institution can see what was asked. Sign-in tokens are stored only as hashes, and the catalogue key behind each one is stored encrypted. Each key has an hourly limit, and may have a daily one and an end date, to keep the catalogue responsive.

Questions and feedback: Dr Johan Pieterse, johan@theahg.co.za.
