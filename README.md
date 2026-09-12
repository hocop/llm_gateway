# LLM Gateway

Simple gateway for serving local models to multiple users.

Main difference from llmlite and bifrost is quota mechanism. Instead of limiting RPM or TPM, this gateway only limits the number concurrent requests to the upstream LLM. It is useful when your tokens don't cost any actual money, but you also don't want one of the users to overload your local LLM serving.

## Configuration



## How to run
