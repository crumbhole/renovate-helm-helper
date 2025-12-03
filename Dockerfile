FROM python:3.14.1
RUN pip install ghapi
RUN pip install GitPython

RUN curl -fsSL -o /tmp/get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 && \
	chmod 700 /tmp/get_helm.sh && \
	/tmp/get_helm.sh && \
    rm /tmp/get_helm.sh
COPY renovate_helper /renovate_helper
CMD ["/renovate_helper", "--log=INFO"]
