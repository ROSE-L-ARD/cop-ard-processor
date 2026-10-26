#show: report.with(
$if(title)$
  title: "$title$",
$else$
  title: "Preliminary Product Validation Report",
$endif$
$if(project-name)$
  project-name: "$project-name$",
$endif$
$if(by-author)$
  author: (
    $for(by-author)$
    (
      name: "$if(it.name.literal)$$it.name.literal$$else$$it.name$$endif$",
      $if(it.affiliations)$
      affiliation: "$for(it.affiliations)$$it.name$$sep$, $endfor$",
      $endif$
    ),
    $endfor$
  ),
$elseif(author)$
  author: (
    $for(author)$
    (
      name: "$if(author.name)$$author.name$$elseif(author.given)$$author.given$ $author.family$$else$$author$$endif$",
      $if(author.affiliation)$
      affiliation: "$if(author.affiliation.name)$$author.affiliation.name$$else$$author.affiliation$$endif$",
      $endif$
    ),
    $endfor$
  ),
$endif$
  logo-header: "figures/B-Open.png",
  logo-title: "figures/AllLogos.png",
$if(contract)$
  contract: "$contract$",
$endif$
$if(refnumber)$
  refnumber: "$refnumber$",
$endif$
$if(issue)$
  issue: "$issue$",
$endif$
$if(revision)$
  revision: "$revision$",
$endif$
$if(issuedate)$
  issuedate: "$issuedate$",
$endif$
$if(status)$
  status: "$status$",
$endif$
$if(approvals)$
  approvals: (
    $for(approvals)$
    ([$it.name$], [$it.date$]),
    $endfor$
  ),
$endif$
$if(changelog)$
  changelog: (
    $for(changelog)$
    ([$it.reason$], [$it.issue$], [$it.revision$], [$it.date$]),
    $endfor$
  ),
$endif$
$if(changerecord)$
  changerecord: (
    $for(changerecord)$
    ([$it.reason$], [$it.date$], [$it.pages$], [$it.paragraphs$]),
    $endfor$
  ),
$endif$
$if(applicabledocuments)$
  applicabledocuments: (
    $for(applicabledocuments)$
    ([$it.id$], [$it.title$], [$it.ref$], [$it.date$]),
    $endfor$
  ),
$endif$
  // Pass reference documents list as content blocks
$if(referencedocuments)$
  referencedocuments: (
    $for(referencedocuments)$
    ([$it.id$], [$it.title$], [$it.ref$], [$it.date$]),
    $endfor$
  ),
$endif$
  // Pass acronyms list as content blocks
$if(acronyms)$
  acronyms: (
    $for(acronyms)$
    ([$it.short$], [$it.long$]),
    $endfor$
  ),
$endif$
)
