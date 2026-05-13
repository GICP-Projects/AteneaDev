# Docker-compose information for the deployment of Elasticsearch + Kibana
Deploy ES + K using `docker-compose up -d`

> ⚠️ **Warning:** Elasticsearch is not currently attached to the same Docker network as Atenea.  
> As a result, you cannot rely on container name resolution to reach Elasticsearch. Instead, you must connect using the Elasticsearch server’s **IP address** and **port number** (e.g., `http://192.168.1.100:9200`) when configuring Atenea’s Elasticsearch settings.  
> 
> If you attempt to use a hostname (for example, `elasticsearch:9200`) without being on the same network, Atenea will not be able to locate the cluster. To avoid connection errors, always specify the full `IP:PORT` until both services share a network.  

> ℹ️ **Note:** You can view the available Elasticsearch Docker image versions for the `STACK_VERSION` variable here:  
> https://hub.docker.com/_/elasticsearch/tags  
> Pick the tag that best fits your compatibility requirements.

## Cert folder

To use the ES Cluster for Atenea, ask an administrator for the public key of the Elasticsearch cluster. This `ca.crt` file should be stored in the `es_cluster/certs` folder.

To copy the certificate from the running container into that folder, run:

```bash
docker exec -it es_cluster_es01_1 \
  cat /usr/share/elasticsearch/config/certs/ca/ca.crt \
  > es_cluster/certs/ca.crt
```
After this, the `es_cluster/certs/ca.crt` file will be available for Atenea to establish a secure connection.


## Obtaining an Elasticsearch API Key

1. In Kibana, go to **Stack Management > Security > API Keys**.
2. Click **Create API key** and fill in any required details.
3. After creation, switch to the **Beat or Logstash** format. This will present the key as `<id>:<api_key>`.  
   Example output: `l9cPNZcBqAtOa2DZeNrV:oxNY2gvbROO0R5kGzwuwxg`
4. Copy the two parts (before and after the colon) into your environment:
```bash
ELASTICSEARCH_APIKEY_ID=l9cPNZcBqAtOa2DZeNrV
ELASTICSEARCH_APIKEY_API_KEY=oxNY2gvbROO0R5kGzwuwxg
```

Atenea currently requires these separate variables in order to authenticate.

> ℹ️ **Note:** There is a TODO to update Atenea so it can accept a single encoded API key instead of splitting it into two values.

## plugins.sh

Is a script to install in the cluster any required plugin.

**Required plugins**:
- None
