
funnel:
	@curl -L -O https://github.com/ohsu-comp-bio/funnel/releases/download/0.10.1/funnel-linux-amd64-0.10.1.tar.gz
	@tar xvzf funnel-linux-amd64-0.10.1.tar.gz funnel

funnel-server: funnel
	@./funnel server run --Logger.OutputFile funnel.logs &

funnel-minio-server: funnel
	@./funnel server run -c tests/funnel-minio.yaml &

ftp-server:
	@docker run -d --name ftpd_server -p 21:21 -p 30000-30099:30000-30099 \
	-e PUBLICHOST=localhost -e FTP_MAX_CLIENTS=50 -e FTP_MAX_CONNECTIONS=50 \
	-e FTP_PASSIVE_PORTS=30000:30099 \
	-e FTP_USER_NAME=bob -e FTP_USER_PASS=12345 -e FTP_USER_HOME=/home/bob \
	stilliard/pure-ftpd

ftp-test:
	@./cwl-tes --tes http://localhost:8000 --insecure \
	--remote-storage-url ftp://bob:12345@localhost \
	tests/hashsplitter-workflow.cwl.yml tests/input.json

ftp-conformance: export CWL_TES_REMOTE_STORAGE = ftp://bob:12345@localhost --insecure

ftp-conformance:
	@./tests/run_conformance.sh

minio-server:
		@docker run -d -p 9000:9000 -v `pwd`/data:/data minio/minio server /data

s3-conformance: export CWL_TES_REMOTE_STORAGE = s3://cwl-tes --endpoint-url http://localhost:9000

s3-conformance:
	@./tests/run_conformance.sh
