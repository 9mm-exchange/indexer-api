#!/bin/bash

# Multi-Chain Token Indexer - Kubernetes Deployment Script
# This script helps deploy the API and Frontend to Kubernetes

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check if kubectl is installed
check_kubectl() {
    if ! command -v kubectl &> /dev/null; then
        print_error "kubectl is not installed. Please install it first."
        exit 1
    fi
    print_success "kubectl is installed"
}

# Function to check cluster connection
check_cluster() {
    print_info "Checking Kubernetes cluster connection..."
    if kubectl cluster-info &> /dev/null; then
        print_success "Connected to Kubernetes cluster"
        kubectl cluster-info | head -n 2
    else
        print_error "Cannot connect to Kubernetes cluster"
        print_info "Please configure kubectl first: kubectl config use-context <your-context>"
        exit 1
    fi
}

# Function to show current context
show_context() {
    CONTEXT=$(kubectl config current-context)
    print_info "Current context: ${GREEN}${CONTEXT}${NC}"
}

# Function to deploy backend
deploy_backend() {
    print_info "Deploying Backend (API)..."
    
    # Apply all backend manifests
    kubectl apply -f k8s/
    
    print_success "Backend resources created/updated"
    
    # Wait for deployment to be ready
    print_info "Waiting for backend deployment to be ready..."
    kubectl wait --for=condition=available --timeout=300s deployment/token-indexer || true
    
    # Show status
    echo ""
    print_info "Backend Status:"
    kubectl get pods -l app=token-indexer
    kubectl get svc token-indexer
    kubectl get pvc token-indexer-data
    
    echo ""
    print_success "Backend deployed successfully!"
    
    # Get LoadBalancer IP
    print_info "Getting LoadBalancer IP (this may take a minute)..."
    sleep 5
    EXTERNAL_IP=$(kubectl get svc token-indexer -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "pending")
    
    if [ "$EXTERNAL_IP" == "pending" ] || [ -z "$EXTERNAL_IP" ]; then
        EXTERNAL_IP=$(kubectl get svc token-indexer -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || echo "pending")
    fi
    
    if [ "$EXTERNAL_IP" != "pending" ] && [ -n "$EXTERNAL_IP" ]; then
        echo ""
        print_success "API is accessible at: ${GREEN}http://${EXTERNAL_IP}${NC}"
        print_info "Health check: ${GREEN}http://${EXTERNAL_IP}/health${NC}"
        print_info "API docs: ${GREEN}http://${EXTERNAL_IP}/docs${NC}"
    else
        echo ""
        print_warning "LoadBalancer IP not yet assigned"
        print_info "Run: ${YELLOW}kubectl get svc token-indexer -w${NC} to watch for IP assignment"
    fi
}

# Function to deploy frontend
deploy_frontend() {
    print_info "Deploying Frontend (Balance Checker)..."
    
    # Create namespace if it doesn't exist
    if ! kubectl get namespace holders &> /dev/null; then
        print_info "Creating namespace: holders"
        kubectl create namespace holders
    else
        print_info "Namespace holders already exists"
    fi
    
    # Create ConfigMap from HTML file
    print_info "Creating ConfigMap from balance-checker.html..."
    kubectl delete configmap balance-checker-html -n holders --ignore-not-found=true
    kubectl create configmap balance-checker-html \
        --from-file=index.html=balance-checker.html \
        -n holders
    
    # Apply frontend manifests
    kubectl apply -f k8s-frontend/
    
    print_success "Frontend resources created/updated"
    
    # Wait for deployment
    print_info "Waiting for frontend deployment to be ready..."
    kubectl wait --for=condition=available --timeout=120s deployment/balance-checker -n holders || true
    
    # Show status
    echo ""
    print_info "Frontend Status:"
    kubectl get pods -n holders
    kubectl get svc -n holders
    kubectl get ingress -n holders 2>/dev/null || print_info "No ingress configured"
    
    echo ""
    print_success "Frontend deployed successfully!"
}

# Function to show logs
show_logs() {
    local component=$1
    
    if [ "$component" == "backend" ]; then
        print_info "Showing backend logs (Ctrl+C to exit)..."
        kubectl logs -f deployment/token-indexer --tail=50
    elif [ "$component" == "frontend" ]; then
        print_info "Showing frontend logs (Ctrl+C to exit)..."
        kubectl logs -f deployment/balance-checker -n holders --tail=50
    else
        print_error "Unknown component: $component"
        print_info "Usage: $0 logs [backend|frontend]"
        exit 1
    fi
}

# Function to show status
show_status() {
    echo ""
    print_info "=== Backend Status ==="
    echo ""
    kubectl get deployment token-indexer
    kubectl get pods -l app=token-indexer
    kubectl get svc token-indexer
    kubectl get pvc token-indexer-data
    
    echo ""
    print_info "=== Frontend Status ==="
    echo ""
    kubectl get deployment balance-checker -n holders 2>/dev/null || print_warning "Frontend not deployed"
    kubectl get pods -n holders 2>/dev/null || true
    kubectl get svc -n holders 2>/dev/null || true
    
    echo ""
}

# Function to delete resources
delete_resources() {
    print_warning "This will delete all deployed resources!"
    read -p "Are you sure? (yes/no): " confirmation
    
    if [ "$confirmation" != "yes" ]; then
        print_info "Deletion cancelled"
        exit 0
    fi
    
    print_info "Deleting backend resources..."
    kubectl delete -f k8s/ --ignore-not-found=true
    
    print_info "Deleting frontend resources..."
    kubectl delete -f k8s-frontend/ --ignore-not-found=true
    kubectl delete configmap balance-checker-html -n holders --ignore-not-found=true
    kubectl delete namespace holders --ignore-not-found=true
    
    print_success "All resources deleted"
}

# Function to port-forward for local access
port_forward() {
    local component=$1
    
    if [ "$component" == "backend" ]; then
        print_info "Port forwarding backend API to localhost:8000..."
        print_info "Access at: ${GREEN}http://localhost:8000${NC}"
        print_warning "Press Ctrl+C to stop"
        kubectl port-forward deployment/token-indexer 8000:8000
    elif [ "$component" == "frontend" ]; then
        print_info "Port forwarding frontend to localhost:8080..."
        print_info "Access at: ${GREEN}http://localhost:8080${NC}"
        print_warning "Press Ctrl+C to stop"
        kubectl port-forward deployment/balance-checker 8080:80 -n holders
    else
        print_error "Unknown component: $component"
        print_info "Usage: $0 port-forward [backend|frontend]"
        exit 1
    fi
}

# Function to restart deployments
restart_deployment() {
    local component=$1
    
    if [ "$component" == "backend" ]; then
        print_info "Restarting backend deployment..."
        kubectl rollout restart deployment/token-indexer
        kubectl rollout status deployment/token-indexer
        print_success "Backend restarted"
    elif [ "$component" == "frontend" ]; then
        print_info "Restarting frontend deployment..."
        kubectl rollout restart deployment/balance-checker -n holders
        kubectl rollout status deployment/balance-checker -n holders
        print_success "Frontend restarted"
    elif [ "$component" == "all" ]; then
        restart_deployment backend
        restart_deployment frontend
    else
        print_error "Unknown component: $component"
        print_info "Usage: $0 restart [backend|frontend|all]"
        exit 1
    fi
}

# Function to show usage
show_usage() {
    cat << EOF
Multi-Chain Token Indexer - Kubernetes Deployment Script

Usage: $0 [command] [options]

Commands:
    deploy-backend          Deploy the API backend
    deploy-frontend         Deploy the frontend
    deploy-all             Deploy both backend and frontend
    status                 Show status of all deployments
    logs [component]       Show logs (backend|frontend)
    restart [component]    Restart deployment (backend|frontend|all)
    port-forward [comp]    Port forward for local access (backend|frontend)
    delete                 Delete all deployed resources
    help                   Show this help message

Examples:
    $0 deploy-all                    # Deploy everything
    $0 logs backend                  # View backend logs
    $0 restart frontend              # Restart frontend
    $0 port-forward backend          # Access backend at localhost:8000
    $0 status                        # Check deployment status

EOF
}

# Main script logic
main() {
    # Check prerequisites
    check_kubectl
    check_cluster
    show_context
    
    echo ""
    
    case "${1:-help}" in
        deploy-backend)
            deploy_backend
            ;;
        deploy-frontend)
            deploy_frontend
            ;;
        deploy-all)
            deploy_backend
            echo ""
            echo "================================================"
            echo ""
            deploy_frontend
            echo ""
            print_success "All components deployed!"
            ;;
        status)
            show_status
            ;;
        logs)
            if [ -z "$2" ]; then
                print_error "Component not specified"
                print_info "Usage: $0 logs [backend|frontend]"
                exit 1
            fi
            show_logs "$2"
            ;;
        restart)
            if [ -z "$2" ]; then
                print_error "Component not specified"
                print_info "Usage: $0 restart [backend|frontend|all]"
                exit 1
            fi
            restart_deployment "$2"
            ;;
        port-forward)
            if [ -z "$2" ]; then
                print_error "Component not specified"
                print_info "Usage: $0 port-forward [backend|frontend]"
                exit 1
            fi
            port_forward "$2"
            ;;
        delete)
            delete_resources
            ;;
        help|--help|-h)
            show_usage
            ;;
        *)
            print_error "Unknown command: $1"
            echo ""
            show_usage
            exit 1
            ;;
    esac
}

# Run main function
main "$@"
