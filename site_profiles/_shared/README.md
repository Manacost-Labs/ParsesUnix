# Shared fragments

Pieces that are genuinely the same across sites, kept in one place so a fix
reaches every profile that uses them.

Deliberately small. A profile is a description of one website's behaviour, and
most of what looks shareable is not: two sites that both paginate do not
paginate the same way. What belongs here is the handful of things that really
are identical — the shape of a cursor loop, the extractor spec for a Next.js
app state blob, a default promotion policy.

The line: a fragment is shared when copying it would mean copying a *bug* too.
Anything else stays in the profile it describes, where it can be read next to
the site it is about.
