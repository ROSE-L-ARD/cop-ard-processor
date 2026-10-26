// ESA Standard Document Template for Typst
// Converted from deltaphi-report.cls

// Suppress empty code blocks (hide mode) emitted by the pandoc typst writer
#let _sl-original = Skylighting
#let Skylighting(fill: none, number: false, start: 1, sourcelines) = {
  if type(sourcelines) != array or sourcelines.len() == 0 { none } else {
    _sl-original(fill: fill, number: number, start: start, sourcelines)
  }
}

#let colors = (
  deepspace: rgb(0, 50, 71),
  deepspace-1: rgb(51, 94, 111),
  deepspace-2: rgb(129, 151, 166),
  deepspace-log: rgb(3, 49, 66),
  tableblue: rgb(204, 229, 255),
  tablegrey: rgb("D9D9D9"),
)
#let author-cell(author, show-affiliation: true) = {
  if type(author) == array {
    author
      .map(a => {
        if type(a) == dictionary {
          if show-affiliation and "affiliation" in a and a.affiliation != "" [
            #a.name (#a.affiliation)
          ] else [
            #a.name
          ]
        } else {
          [#a]
        }
      })
      .join([, ])
  } else if type(author) == dictionary {
    if show-affiliation and "affiliation" in author and author.affiliation != "" [
      #author.name (#author.affiliation)
    ] else [
      #author.name
    ]
  } else {
    [#author]
  }
}

#let report(
  title: "",
  project-name: "",
  author: "",
  doctype: "Technical Note",
  securitymarking: "Public Distribution",
  contract: "4000144699/24/I-NS",
  refnumber: "esa-rose-l-018-D18",
  issue: "1",
  revision: "0",
  issuedate: "2026-10-26",
  status: "Approved",
  logo-header: none,
  logo-title: none,
  approvals: (),
  changelog: (),
  changerecord: (),
  body,
) = {
  // Page geometry and margins matching deltaphi-report.cls
  set page(
    paper: "a4",
    margin: (
      top: 4.23cm,
      bottom: 2.0cm,
      left: 2.0cm,
      right: 1.5cm,
    ),
    header: context {
      let page-num = counter(page).get().first()
      set text(lang: "und")
      set par(justify: false)
      // header on every page incl. cover (cls pagestyle fancy everywhere)
      grid(
        columns: (4.2cm, 1fr, 5.5cm),
        align: (left + horizon, center + horizon, right + horizon),
        if logo-header != none {
          image(logo-header, width: 4.2cm)
        } else {
          text(fill: colors.deepspace, weight: "bold")[B-Open]
        },
        text(size: 14pt, weight: "bold", fill: colors.deepspace)[#title],
        text(size: 8pt, fill: black)[
          Contract Nº: #contract \
          Reference: #refnumber \
          Issue/Revision: #issue.#revision \
          Date of Issue: #issuedate
        ],
      )
    },
    footer: context {
      let page-num = counter(page).get().first()
      let total-pages = counter(page).final().first()
      // footer on every page incl. cover (securitymarking); page number only past cover
      grid(
        columns: (1fr, 3fr, 1fr),
        align: (left + bottom, center + bottom, right + bottom),
        if page-num == 1 { [] } else {
          text(size: 8pt, fill: colors.deepspace-1)[
            Page #page-num/#total-pages
          ]
        },
        text(size: 8pt, fill: colors.deepspace)[
          #securitymarking
        ],
        [],
      )
    },
  )

  // Typography & Paragraph formatting
  set text(
    font: ("Liberation Sans",),
    size: 11pt,
    spacing: 115%,
    lang: "en",
  )
  set par(
    justify: true,
    leading: 0.75em,
    spacing: 1.2em,
  )

  // Heading styling
  set heading(numbering: "1.1. ")
  show heading.where(level: 1): it => {
    set text(fill: colors.deepspace-1, size: 16pt, weight: "bold")
    v(18pt, weak: true)
    upper(it)
    v(12pt, weak: true)
  }
  show heading.where(level: 2): it => {
    set text(fill: colors.deepspace, size: 14pt, weight: "bold")
    v(14pt, weak: true)
    it
    v(10pt, weak: true)
  }
  show heading.where(level: 3): it => {
    set text(fill: colors.deepspace-1, size: 12pt, weight: "bold", style: "italic")
    v(12pt, weak: true)
    it
    v(8pt, weak: true)
  }

  // ---------------------------------------------------------
  // TITLE PAGE
  // ---------------------------------------------------------
  v(7.0cm)

  align(center)[
    #text(fill: colors.deepspace-1, size: 15pt, weight: "bold")[#upper(project-name)] \
    #v(0.4cm)
    #set par(justify: false)
    #text(fill: colors.deepspace-1, size: 14pt, weight: "bold")[#upper(title)]
  ]

  v(1fr)

  block[
    #set text(fill: colors.deepspace-1, size: 9pt)
    #table(
      columns: (4.2cm, 8.7cm),
      stroke: none,
      row-gutter: 0.6em,
      [Prepared by], [#author-cell(author)],
      [Contract Nº], [#contract],
      [Reference], [#refnumber],
      [Issue/Revision], [#issue.#revision],
      [Date of Issue], [#issuedate],
      [Status], [#status],
    )
  ]

  v(1.3cm)

  v(1fr)

  if logo-title != none {
    align(center + bottom, image(logo-title, width: 15.0cm))
  }

  pagebreak()

  // ---------------------------------------------------------
  // FRONTMATTER / LOG PAGE
  // ---------------------------------------------------------

  // Approval Table
  text(fill: colors.deepspace-2, size: 22pt, weight: "bold")[APPROVAL]
  v(0.3cm)
  {
    set text(fill: colors.deepspace-log, size: 9pt)
    table(
      columns: (2.5cm, 1fr, 3.0cm, 1fr),
      stroke: (x, y) => (
        top: 0.5pt + colors.deepspace-log,
        bottom: 0.5pt + colors.deepspace-log,
        left: if x == 2 { 0.5pt + colors.deepspace-log } else { none },
        right: none,
      ),
      [Title], table.cell(colspan: 3)[#title],
      [Issue Number], [#issue], [Revision Number], [#revision],
      [Author], [#author-cell(author)], [Date], [#issuedate],
    )
    v(-8pt)
    table(
      columns: (1fr, 1fr),
      stroke: (x, y) => (
        top: 0.5pt + colors.deepspace-log,
        bottom: 0.5pt + colors.deepspace-log,
        left: if x == 1 { 0.5pt + colors.deepspace-log } else { none },
        right: none,
      ),
      table.header([*Approved by*], [*Date of Approval*]),
      ..for entry in approvals {
        (entry.at(0), entry.at(1))
      },
    )
  }

  v(1.2cm)

  // Change Log Table
  text(fill: colors.deepspace-2, size: 22pt, weight: "bold")[CHANGE LOG]
  v(0.3cm)
  {
    set text(fill: colors.deepspace-log, size: 9pt)
    table(
      columns: (1fr, 2.2cm, 2.8cm, 2.5cm),
      stroke: (x, y) => (
        top: 0.5pt + colors.deepspace-log,
        bottom: 0.5pt + colors.deepspace-log,
        left: if x > 0 { 0.5pt + colors.deepspace-log } else { none },
        right: none,
      ),
      table.header([*Reason for change*], [*Issue Nr.*], [*Revision Number*], [*Date*]),
      ..for entry in changelog {
        (entry.at(0), str(entry.at(1)), str(entry.at(2)), entry.at(3))
      },
    )
  }

  v(1.2cm)

  // Change Record Table
  text(fill: colors.deepspace-2, size: 22pt, weight: "bold")[CHANGE RECORD]
  v(0.3cm)
  {
    set text(fill: colors.deepspace-log, size: 9pt)
    table(
      columns: (1fr, 2.5cm, 2.2cm, 2.2cm),
      stroke: (x, y) => (
        top: 0.5pt + colors.deepspace-log,
        bottom: 0.5pt + colors.deepspace-log,
        left: if x > 0 { 0.5pt + colors.deepspace-log } else { none },
        right: none,
      ),
      [Issue Number: #issue], table.cell(colspan: 3)[Revision Number: #revision],
      table.header([*Reason for change*], [*Date*], [*Page(s)*], [*Paragraph(s)*]),
      ..for entry in changerecord {
        (entry.at(0), entry.at(1), entry.at(2), entry.at(3))
      },
    )
  }

  pagebreak()

  // ---------------------------------------------------------
  // TABLE OF CONTENTS
  // ---------------------------------------------------------
  show outline.entry.where(level: 1): it => {
    v(10pt, weak: true)
    strong(it)
  }
  outline(
    title: text(fill: colors.deepspace, size: 14pt, weight: "bold")[Table of Contents],
    indent: auto,
    depth: 3,
  )

  pagebreak()

  body
}

