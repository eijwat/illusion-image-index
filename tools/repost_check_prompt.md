# Visual repost check (instructions for the checking agents)

We are about to publish image files under the licence their uploader stated (Creative Commons / public domain).
The danger is an upload whose uploader is NOT the author: a repost of someone else's work with a licence the
author never gave. Your job: look at each tile and its text, and decide whether the file can safely go into the
public zip. **When in doubt, exclude.** Excluding a good picture costs little; publishing a repost is a real harm.

Each sheet `sheet_NNNN.png` has up to 6 numbered tiles (animations show 4 frames, marked ANIM). The file
`sheet_NNNN.txt` gives, per tile number, the source records: source site, title, author, credit, licence, page.

Answer **exclude** if any of these applies or might apply:
1. A well-known illusion artwork or design by a known artist or scientist (e.g. Kitaoka's rotating snakes and his
   other drift / colour works, Escher, Vasarely, Bridget Riley, Ocampo, Gonsalves, Del-Prete, Magritte, Dalí,
   Ōuchi, Shepard, Adelson's checker shadow, Pinna, Lotto, Sugihara's objects, Akiyoshi-style designs), unless
   the text shows the uploader is that artist. A plain textbook redrawing of a classical figure (Müller-Lyer,
   Ponzo, Hering, Zöllner, Kanizsa triangle, Necker cube, Rubin vase, café wall, Hermann grid…) made by the
   uploader is fine.
2. A scan or screenshot of a book, magazine, poster, web page, app, video, game, TV, advert or product
   packaging, unless it is clearly old enough to be public domain (e.g. a 19th-century engraving) and the
   licence says public domain.
3. A photograph whose main subject is someone else's artwork that is probably still protected: a modern mural,
   3-D street painting, trompe-l'oeil wall, sculpture, installation, exhibit in an illusion museum, poster or
   painting by a 20th/21st-century artist. (Photos of buildings, landscapes, everyday scenes, or of artworks
   clearly older than ~1900 are fine.)
4. A watermark, logo, signature or web address of someone other than the stated author; a collage of images
   from elsewhere; a meme; a stock-photo look with a stock watermark.
5. You cannot see the tile, or you cannot tell what it is and where it comes from.

Otherwise answer **keep** (the uploader plausibly made it: own drawing, own photo, own stimulus, an old
public-domain work).

Write your answers as a JSON array to the file you are given, one object per tile, for every tile of every
sheet you were assigned:

    {"sheet": 12, "number": 3, "decision": "keep" | "exclude", "reason": "short reason (required for exclude)"}

Do not open any other files. Do not use the web. Return only a one-line summary (counts of keep / exclude).
