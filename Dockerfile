FROM python:3.14.6
# ghapi 2.x went async and this script didn't.
RUN pip install 'ghapi<2'
RUN pip install GitPython

RUN curl -fsSL -o /tmp/get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 && \
	chmod 700 /tmp/get_helm.sh && \
	/tmp/get_helm.sh && \
    rm /tmp/get_helm.sh
COPY renovate_helper /renovate_helper
CMD ["/renovate_helper", "--log=INFO"]
