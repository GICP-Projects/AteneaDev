#!/bin/bash

nodes=("es_cluster_es01_1" "es_cluster_es02_1" "es_cluster_es03_1")
plugin=""

for node in "${nodes[@]}"
do
    echo "Instalando plugin en $node..."
    docker exec -it "$node" bin/elasticsearch-plugin install "$plugin"
    echo "Reiniciando $node..."
    docker restart "$node"
done

