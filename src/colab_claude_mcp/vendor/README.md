# Third-party code used by the mirror pages

The Colab and Jupyter mirror pages render Markdown cells (tables, images, raw HTML) and LaTeX math with
these libraries. They are served by the hub at `/vendor/...`, so the pages work offline and load no
scripts from other sites. The files are copied unchanged from the npm packages below; each folder keeps
the package's license. The folder names carry the version, so browsers may cache the files for a long time.

| Folder | Package | License | Files taken |
|---|---|---|---|
| `marked-18.2.0/` | [marked](https://github.com/markedjs/marked) 18.2.0 | MIT | `lib/marked.umd.js`, `LICENSE` |
| `dompurify-3.4.18/` | [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.18 | Apache-2.0 (or MPL-2.0) | `dist/purify.min.js`, `LICENSE` |
| `katex-0.19.1/` | [KaTeX](https://github.com/KaTeX/KaTeX) 0.19.1 | MIT | `dist/katex.min.js`, `dist/katex.min.css`, the 20 `dist/fonts/*.woff2`, `LICENSE` |

The tarballs came from registry.npmjs.org and matched the `dist.integrity` the registry publishes:

- `marked-18.2.0.tgz`: `sha512-KaeL+tAbtvI930q7HE2PN5lYXYdT8EiSmxHlN6F3rsfb57wd5mp5W5TLazpdj4inTQBJdgjYwbs40W/0VUGU+Q==`
- `dompurify-3.4.18.tgz`: `sha512-ofe3wI/uNm68HInABYFJYqQxRw1EaKAjNdrFVKqjUY+UZurvNd2L/HuQe097s/4RivWiZyP6tT7aoT8atjfhwg==`
- `katex-0.19.1.tgz`: `sha512-jG8uKntjSEcm2EpQOh0XturpPuuC9r3Sq5HLHdsFdWZT27E672SF19QEi+of0/YeOy15lDgRhKvr5614uRrEfA==`

`katex.min.css` also names `.woff` and `.ttf` fonts after each `.woff2`; browsers that read woff2 never
ask for them, so they are left out.

To update one: download the new tarball from the registry, check it against `dist.integrity`, copy the
same files into a folder named after the new version, delete the old folder, and change the version in
the `<script>`/`<link>` tags of `mirror.html` and in `VENDOR_FILES` of `tests/test_hub.py`.
