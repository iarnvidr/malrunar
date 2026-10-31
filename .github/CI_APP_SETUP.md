# Weekly pin update automation

The weekly pin bump opens or updates a pull request. It uses a repository scoped GitHub App token so the normal pull request checks run for the generated branch.

Configure a GitHub App installed on the `iarnvidr/malrunar` repository only. Give it these repository permissions:

- **Contents:** Read and write
- **Pull requests:** Read and write

The workflow requests only those permissions and limits its token to this repository. It does not need organization access, Actions administration, packages, or secrets beyond the App private key.

Set these repository-level values:

- [Actions variables](https://github.com/iarnvidr/malrunar/settings/variables/actions): `CI_APP_ID` = the App ID.
- [Actions secrets](https://github.com/iarnvidr/malrunar/settings/secrets/actions): `CI_APP_PRIVATE_KEY` = the PEM contents of a private key generated for that App.

The weekly workflow stops with a clear error when either value is missing. The private key stays in the repository secret store; it is never written to the repository or emitted in logs.
