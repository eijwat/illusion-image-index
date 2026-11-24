# illusion-image-index

An index of visual-illusion images published on the web. It lists 47,413 pictures (60,362 source records from
29 sources: Wikimedia Commons, Flickr via Openverse, Visiome, university and artist sites, blogs and datasets).
Each picture has:
- a label: illusion / not an illusion / trick art / unsure;
- perceptual categories, for 11,421 illusion pictures;
- its source URLs, authors, dates and stated licences;
- perceptual hashes;
- groupings of variants of the same work.

The labels were made by Claude (Anthropic) from the image alone, and checked against blind human judgements.

- Paper: (arXiv link to be added)
- Archived release with images: (Zenodo DOI to be added)
- Author: Eiji Watanabe, National Institute for Basic Biology, Okazaki, Japan
  ([ORCID 0000-0001-9089-8122](https://orcid.org/0000-0001-9089-8122))

**This repository holds no images.** Most illusion images on the web may not be redistributed. For those, the
index gives the facts and a link to the page where the image is published. Images that may be redistributed, and
that passed a repost screen (below), are bundled in the Zenodo record in two zips:

| zip | files | size | licences |
|---|---|---|---|
| `images-free.zip` | 2,163 | 7.4 GB | public domain, CC0, CC BY, CC BY-SA, FAL |
| `images-nc.zip` | 366 | 0.18 GB | CC with NonCommercial and/or NoDerivatives |

Each zip has one folder per label (`illusion/`, `unsure/`, `trick/`). Files are named `<sha256>.<ext>`.
`ATTRIBUTION.csv` gives the source page, author and licence of every file. Every file keeps its own licence.

## Labels

| label | pictures | meaning |
|---|---|---|
| `illusion` | 15,304 | the answer lies in the eye or brain: the picture looks different from what it is, even when you know |
| `trick` | 5,738 | trick art: the answer is clear once seen (trompe-l'oeil, hidden pictures, pareidolia, double images) |
| `not_illusion` | 24,582 | anything else collected by the crawler (photos, diagrams, logos, ...) |
| `unsure` | 1,789 | the judge could not decide |

Ambiguous figures (Necker cube, Rubin's vase, duck–rabbit) and hybrid images are labelled `illusion`, because the
percept keeps switching even when the observer knows the answer. Faces made of objects, hidden pictures and
camouflage are labelled `trick`.

Categories (`categories.csv`, several allowed per picture, plus a main one):

| code | category |
|---|---|
| G | geometric (size, length, angle, shape) |
| L | lightness and contrast |
| C | colour |
| M | motion |
| D | depth and 3-D shape |
| K | completion and illusory contours |
| A | ambiguous figures |
| I | impossible figures |
| F | face and object recognition |
| R | afterimage, adaptation and fading |
| X | multimodal (vision with sound or touch) |
| T | also trick art (only together with an illusion category) |
| ? | category unclear |

Pictures are left out of the classification (`in_classification = 0`) in two cases:
- they fit inside 320×240 px;
- they are a non-representative variant of a work.

## Tables (`data/`)

| file | one row per | main columns |
|---|---|---|
| `pictures.csv` | picture (47,413) | `label` and the judge's short note; `small`; `work`, `work_rep`; `in_classification`; `category_main`, `categories`, `illusion_name`; `demo` (demonstration or experiment photo); `sources`; `zip`; `no_ai_input`; `flags` |
| `records.csv` | source record (60,362) | `source`, `page_url`, `file_url`, `sha256`, `picture`, `title`, `author`, `credit`, `date`; `licence_stated` and `licence` (normalised); `licence_class` (`free` / `nc` / `none`); `flags` |
| `files.csv` | distinct file (55,468) | `sha256`, `picture`, size, frames; `dhash`, `ahash` (64-bit perceptual hashes); `zip`; `zip_note` (why a licensed file is not bundled) |
| `concepts.csv` | illusion concept (353, from Wikidata) | English and Japanese names and descriptions, discoverer, year, `core` |
| `record_concepts.csv` | record–concept link | `method` (how the link was made) |
| `check_labels.csv` | blind human check of labels (300) | `stratum` (the first-round label the sample was drawn from, 100 each), Claude's first-round and final labels, the human label |
| `check_categories.csv` | blind human check of categories (351) | Claude's categories vs the human categories |
| `repost_check.csv` | visually checked picture (2,816) | `decision` (keep / exclude) and reason |
| `categories.csv` | category code | English and Japanese names |

A *picture* groups files that are the same image at different sizes or formats. Its id is the SHA-256 of the
largest file. A *work* groups different versions of the same artwork, such as colour variants, crops or panels. A
*record* is one listing of a file by one source. Joins: `records.picture` → `pictures.picture`, and `files.sha256`
→ `records.sha256`.

Descriptions written by the sources are not included (they are copyrighted text). Follow `page_url` to read them.

## Sources

The collection was made in October 2026. The crawler followed robots.txt, kept long intervals between requests and
identified itself in the User-Agent. Animated GIFs are included; videos are not.

| source | records | images bundled? |
|---|---|---|
| Akiyoshi Kitaoka's pages (psy.ritsumei.ac.jp) | 21,208 | no (links only) |
| Flickr via Openverse (CC) | 12,258 | some (see screen) |
| Mighty Optical Illusions (moillusions.com) | 9,072 | no |
| Wikimedia Commons, *Optical illusions* category tree | 7,261 | some |
| Illusionen.org | 1,783 | no |
| Visiome Platform (visiome.neuroinf.jp, CC by the authors) | 1,268 | yes, where licensed |
| ViperLib (via the Internet Archive) | 1,133 | no |
| Kodaka Lab, Archimedes Lab, Sugihara, Anstis Lab, Arai, NTT Illusion Forum, Illusion of the Year, Novick, Illusion Science, IllusionWorks, OpticalIllusion.net, Panoptikum, EyeTricks, benricho, i-zukan, the Illusion Contest (sakkon) | 5,207 | no |
| IllusionVQA dataset | 450 | no |
| Journal of Illusion (CC BY-NC-ND) | 265 | yes |
| The Illusions Index | 174 | where licensed |
| Michael Bach's pages | 172 | no (also `no_ai_input`) |
| Project Gutenberg (Luckiesh 1922) | 102 | yes |
| Wikidebrouillard | 9 | yes |

**Michael Bach's site** asks that its content not be used as AI input (`Content-signal: ai-input=no`). Its 172
records (124 pictures) carry `no_ai_input`. Do not use them to train models.

## Repost screen

A licensed file is bundled only if all of the following hold:
1. It is not labelled `not_illusion`.
2. The same picture or work is not also published by a source that forbids redistribution, unless the licensed
   copy comes from the author's own release (Visiome, Journal of Illusion, Gutenberg).
3. Its perceptual hashes are not within 10 bits of a non-redistributable picture.
4. It is not a Flickr upload of an illusion or trick picture (the uploader may not be the author).
5. On Commons, it is marked as the uploader's own work or as public domain.
6. Its metadata names no artist whose illusion works are protected.
7. It passed a visual check: Claude looked at every remaining illusion, trick and unsure picture together with its
   source text, and excluded anything that might be someone else's work. Examples are photos of modern murals,
   street paintings or museum exhibits, scans of books or screens, logos, and designs by known artists.

Rules 2–6 were applied first; the visual check (rule 7) covered the 2,816 illusion, trick and unsure pictures that
passed them. Files that fail are listed with the reason in `files.zip_note`. If you find a file that should not be
there, please open an issue.

## Reproduce

Python 3.12 or later with `requests` and `Pillow` (no other packages).

- `crawler/` is the collection code (`python3 -m illusions <command>`, settings in `settings.example.toml`).
  Running it again collects the web as it is now, not as it was in October 2026.
- `tools/export.py` writes `data/` from the working database.
- `tools/zips.py` builds the image zips.
- `tools/repost_check_prompt.md` holds the instructions given to the visual check.

## Licence

| part | licence |
|---|---|
| Code (`crawler/`, `tools/`) | MIT (`LICENSE`) |
| Metadata tables, labels and categories (`data/`) | CC BY 4.0 (`LICENSE-DATA`) |
| Images in the Zenodo zips | each file's own licence (`ATTRIBUTION.csv`) |

## Citation

See `CITATION.cff`.
